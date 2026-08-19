from __future__ import annotations

import json
import multiprocessing
import os
import sqlite3
import stat
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from core.ops.cognitive_data_contract import (
    CognitiveDataEvent,
    now_utc,
    stable_dedupe_key,
    stable_event_id,
)
from core.ops.durable_io import DurableIOError
from core.ops.producer_consumer_ledger import ProducerConsumerLedger
from core.ops.runtime_flow_telemetry import RuntimeFlowTelemetry, record_runtime_produced


def _spool_from_child(database_dir, payload, started):
    started.set()
    RuntimeFlowTelemetry(SimpleNamespace(database_dir=database_dir))._spool(
        payload
    )


def _quarantine_from_child(database_dir, payload, barrier):
    from core.ops import runtime_flow_telemetry as telemetry_module

    original_write = os.write

    def bytewise_write(descriptor, data):
        written = original_write(descriptor, data[:1])
        time.sleep(0.0001)
        return written

    telemetry_module.os.write = bytewise_write
    barrier.wait(timeout=5)
    RuntimeFlowTelemetry(
        SimpleNamespace(database_dir=database_dir)
    )._quarantine(payload, error_type="Injected")


def _config(tmp_path):
    return SimpleNamespace(database_dir=tmp_path)


def test_runtime_telemetry_records_real_production_and_terminal_receipt(tmp_path):
    cfg = _config(tmp_path)
    ledger = ProducerConsumerLedger(cfg, initialize=True)
    ledger.register_flow(
        flow_id="real_flow",
        data_type="event",
        producer_refs=["producer"],
        consumer_refs=["consumer"],
    )
    telemetry = RuntimeFlowTelemetry(cfg)

    production_event_id = telemetry.produced(
        "real_flow",
        source="producer",
        item_id="item-1",
        intended_consumers=["consumer"],
    )
    telemetry.consumed(
        "real_flow",
        source="consumer",
        item_id="item-1",
        production_event_id=production_event_id,
    )

    flow = ledger.snapshot()["flows"]["real_flow"]
    assert flow["observation_state"] == "observed"
    assert flow["terminal_consumer_count"] == 1


def test_runtime_telemetry_spools_and_replays_when_ledger_write_fails(tmp_path, monkeypatch):
    cfg = _config(tmp_path)
    ledger = ProducerConsumerLedger(cfg, initialize=True)
    ledger.register_flow(
        flow_id="compensated_flow",
        data_type="event",
        producer_refs=["producer"],
        consumer_refs=["consumer"],
    )
    telemetry = RuntimeFlowTelemetry(cfg)
    original = ProducerConsumerLedger.record_produced

    def fail_once(self, *args, **kwargs):
        monkeypatch.setattr(ProducerConsumerLedger, "record_produced", original)
        raise sqlite3.OperationalError("simulated ledger lock")

    monkeypatch.setattr(ProducerConsumerLedger, "record_produced", fail_once)

    assert (
        telemetry.produced(
            "compensated_flow",
            source="producer",
            item_id="item-1",
            intended_consumers=["consumer"],
        )
        is None
    )
    assert telemetry.outbox_path.is_file()
    assert stat.S_IMODE(telemetry.outbox_path.stat().st_mode) == 0o600

    replayed = telemetry.drain_outbox()
    telemetry.consumed(
        "compensated_flow",
        source="consumer",
        item_id="item-1",
    )

    assert replayed == 1
    assert not telemetry.outbox_path.exists()
    assert ledger.snapshot()["flows"]["compensated_flow"]["observation_state"] == "observed"


def test_uninspectable_outbox_never_becomes_empty_or_allows_direct_reordering(
    tmp_path,
    monkeypatch,
):
    telemetry = RuntimeFlowTelemetry(_config(tmp_path))
    original_stat = Path.stat

    def denied(path, *args, **kwargs):
        if path == telemetry.outbox_path:
            raise PermissionError("sentinel")
        return original_stat(path, *args, **kwargs)

    monkeypatch.setattr(Path, "stat", denied)
    monkeypatch.setattr(
        telemetry,
        "_apply_payload",
        lambda _payload: (_ for _ in ()).throw(
            AssertionError("uninspectable outbox must block direct ledger apply")
        ),
    )
    monkeypatch.setattr(
        telemetry,
        "_spool",
        lambda _payload: (_ for _ in ()).throw(
            AssertionError("uninspectable outbox must not be overwritten")
        ),
    )

    assert telemetry._record_or_spool_state({"operation": "sentinel"}) == (
        None,
        False,
    )
    with pytest.raises(
        DurableIOError,
        match="runtime_flow_outbox_inspection_failed",
    ):
        telemetry.drain_outbox()


def test_outbox_preserves_producer_order_when_oldest_event_cannot_replay(
    tmp_path, monkeypatch
):
    cfg = _config(tmp_path)
    ledger = ProducerConsumerLedger(cfg, initialize=True)
    ledger.register_flow(
        flow_id="ordered_flow",
        data_type="event",
        producer_refs=["producer"],
        consumer_refs=["consumer"],
    )
    telemetry = RuntimeFlowTelemetry(cfg)
    original = ProducerConsumerLedger.record_produced

    def fail_oldest(self, *args, **kwargs):
        if kwargs.get("item_id") == "item-1":
            raise sqlite3.OperationalError("oldest event remains locked")
        return original(self, *args, **kwargs)

    monkeypatch.setattr(ProducerConsumerLedger, "record_produced", fail_oldest)

    assert telemetry.produced(
        "ordered_flow",
        source="producer",
        item_id="item-1",
        intended_consumers=["consumer"],
    ) is None
    assert telemetry.produced(
        "ordered_flow",
        source="producer",
        item_id="item-2",
        intended_consumers=["consumer"],
    ) is None

    with sqlite3.connect(ledger.db_path) as conn:
        produced = conn.execute(
            "SELECT COUNT(*) FROM runtime_flow_events WHERE direction='produced'"
        ).fetchone()[0]
    assert produced == 0
    assert len(telemetry.outbox_path.read_text(encoding="utf-8").splitlines()) == 2


def test_outbox_deduplicates_identical_pending_payload(tmp_path):
    cfg = _config(tmp_path)
    ProducerConsumerLedger(cfg, initialize=True)
    telemetry = RuntimeFlowTelemetry(cfg)
    payload = {
        "operation": "dead_letter",
        "flow_id": "dedupe-flow",
        "source": "consumer",
        "item_id": "item-1",
        "production_event_id": "event-1",
        "metadata": {"reason": "same failure"},
        "generation_id": "generation-1",
        "idempotency_key": "dedupe-flow:generation-1:terminal",
    }

    telemetry._spool(payload)  # noqa: SLF001 - exact durable dedupe oracle
    telemetry._spool(payload)  # noqa: SLF001 - exact durable dedupe oracle

    assert (
        len(telemetry.outbox_path.read_text(encoding="utf-8").splitlines())
        == 1
    )


def test_outbox_drain_preserves_concurrent_process_append(
    tmp_path,
    monkeypatch,
):
    telemetry = RuntimeFlowTelemetry(_config(tmp_path))
    first = {
        "operation": "dead_letter",
        "flow_id": "concurrent-flow",
        "source": "consumer",
        "item_id": "item-a",
    }
    second = {
        "operation": "dead_letter",
        "flow_id": "concurrent-flow",
        "source": "consumer",
        "item_id": "item-b",
    }
    telemetry._spool(first)  # noqa: SLF001 - durable race setup
    started = multiprocessing.Event()
    child = None

    def apply_first(_payload):
        nonlocal child
        child = multiprocessing.Process(
            target=_spool_from_child,
            args=(tmp_path, second, started),
        )
        child.start()
        assert started.wait(timeout=5)
        return "receipt-a"

    monkeypatch.setattr(telemetry, "_apply_payload", apply_first)

    assert telemetry.drain_outbox() == 1
    assert child is not None
    child.join(timeout=5)
    assert child.exitcode == 0
    lines = telemetry.outbox_path.read_text(encoding="utf-8").splitlines()
    assert [json.loads(line)["item_id"] for line in lines] == ["item-b"]


def test_cognitive_event_durable_spool_survives_short_writes(
    tmp_path,
    monkeypatch,
):
    from core.ops import runtime_flow_telemetry as telemetry_module

    telemetry = RuntimeFlowTelemetry(_config(tmp_path))
    event = CognitiveDataEvent(
        event_id="cde-short-write",
        source_kind="sync_engine",
        source_uri="sync://session/short-write",
        content_hash="hash",
        canonical_subject="session:short-write",
        data_type="synced_turn",
        producer="sync_engine",
        intended_consumers=("distill",),
        privacy_level="local",
        confidence=1.0,
        evidence_refs=("raw-revision:short-write",),
        dedupe_key="short-write",
        created_at=now_utc(),
    )
    monkeypatch.setattr(
        telemetry,
        "_apply_payload",
        MagicMock(side_effect=sqlite3.OperationalError("locked")),
    )
    original_write = os.write
    fsynced_directories = []
    monkeypatch.setattr(
        telemetry_module,
        "fsync_directory",
        lambda path: fsynced_directories.append(path),
    )

    def short_write(descriptor, data):
        return original_write(
            descriptor,
            data[: max(1, len(data) // 2)],
        )

    monkeypatch.setattr(os, "write", short_write)

    assert telemetry.cognitive_event(event) == event.event_id
    payload = json.loads(
        telemetry.outbox_path.read_text(encoding="utf-8")
    )
    assert payload["event"]["event_id"] == event.event_id
    assert fsynced_directories == [tmp_path]


def test_cognitive_event_does_not_claim_spool_when_parent_fsync_fails(
    tmp_path,
    monkeypatch,
):
    from core.ops import runtime_flow_telemetry as telemetry_module

    telemetry = RuntimeFlowTelemetry(_config(tmp_path))
    event = CognitiveDataEvent(
        event_id="cde-parent-fsync-failure",
        source_kind="sync_engine",
        source_uri="sync://session/parent-fsync-failure",
        content_hash="hash",
        canonical_subject="session:parent-fsync-failure",
        data_type="synced_turn",
        producer="sync_engine",
        intended_consumers=("distill",),
        privacy_level="local",
        confidence=1.0,
        evidence_refs=("raw-revision:parent-fsync-failure",),
        dedupe_key="parent-fsync-failure",
        created_at=now_utc(),
    )
    monkeypatch.setattr(
        telemetry,
        "_apply_payload",
        MagicMock(side_effect=sqlite3.OperationalError("locked")),
    )
    monkeypatch.setattr(
        telemetry_module,
        "fsync_directory",
        MagicMock(side_effect=OSError("directory fsync failed")),
    )

    with pytest.raises(OSError, match="directory fsync failed"):
        telemetry.cognitive_event(event)


def test_dead_letter_outbox_serializes_cross_process_short_writes(tmp_path):
    barrier = multiprocessing.Barrier(2)
    payloads = [
        {
            "operation": "cognitive_consumed",
            "event_id": f"cde-concurrent-dead-{index}",
            "consumer_id": "distill",
        }
        for index in range(2)
    ]
    processes = [
        multiprocessing.Process(
            target=_quarantine_from_child,
            args=(tmp_path, payload, barrier),
        )
        for payload in payloads
    ]
    for process in processes:
        process.start()
    for process in processes:
        process.join(timeout=10)
        assert process.exitcode == 0

    dead_letter_path = tmp_path / "runtime_flow_outbox.dead_letter.jsonl"
    rows = [
        json.loads(line)
        for line in dead_letter_path.read_text(encoding="utf-8").splitlines()
    ]
    assert len(rows) == 2
    assert {row["event_id"] for row in rows} == {
        "cde-concurrent-dead-0",
        "cde-concurrent-dead-1",
    }


def test_cognitive_telemetry_records_event_and_all_terminal_consumers(tmp_path):
    cfg = _config(tmp_path)
    ProducerConsumerLedger(cfg, initialize=True)
    telemetry = RuntimeFlowTelemetry(cfg)
    event = CognitiveDataEvent(
        event_id=stable_event_id("capture", "session", "hash"),
        source_kind="raw_capture",
        source_uri="capture://session/turn-1",
        content_hash="hash",
        canonical_subject="session",
        data_type="conversation_turn",
        producer="capture_service",
        intended_consumers=("capture_queue",),
        privacy_level="local",
        confidence=1.0,
        evidence_refs=("raw-revision:1",),
        dedupe_key=stable_dedupe_key("raw_capture", "session", "hash"),
        created_at=now_utc(),
    )

    telemetry.cognitive_event(event)
    telemetry.cognitive_consumed(
        event.event_id,
        consumer_id="capture_queue",
        outcome="queued",
    )
    snapshot = ProducerConsumerLedger(cfg, initialize=True).cognitive_data_snapshot()
    assert snapshot["status"] == "ok"
    assert snapshot["counts"]["intended_consumptions"] == 1
    assert snapshot["counts"]["terminal_consumptions"] == 1


def test_cognitive_event_returns_id_only_after_durable_spool(
    tmp_path,
    monkeypatch,
):
    cfg = _config(tmp_path)
    ProducerConsumerLedger(cfg, initialize=True)
    telemetry = RuntimeFlowTelemetry(cfg)
    event = CognitiveDataEvent(
        event_id="cde-durable-spool",
        source_kind="sync_engine",
        source_uri="sync://session/turn-1",
        content_hash="hash",
        canonical_subject="session:turn-1",
        data_type="synced_turn",
        producer="sync_engine",
        intended_consumers=("distill",),
        privacy_level="local",
        confidence=1.0,
        evidence_refs=("raw-revision:1",),
        dedupe_key="durable-spool",
        created_at=now_utc(),
    )
    original = ProducerConsumerLedger.record_data_event

    def fail_once(self, *args, **kwargs):
        monkeypatch.setattr(
            ProducerConsumerLedger,
            "record_data_event",
            original,
        )
        raise sqlite3.OperationalError("simulated ledger lock")

    monkeypatch.setattr(
        ProducerConsumerLedger,
        "record_data_event",
        fail_once,
    )

    assert telemetry.cognitive_event(event) == event.event_id
    assert telemetry.outbox_path.is_file()
    assert telemetry.drain_outbox() == 1
    assert not telemetry.outbox_path.exists()


def test_permanent_cognitive_receipt_conflict_is_quarantined_not_retried(tmp_path):
    cfg = _config(tmp_path)
    ledger = ProducerConsumerLedger(cfg, initialize=True)
    telemetry = RuntimeFlowTelemetry(cfg)
    event = CognitiveDataEvent(
        event_id="cde-permanent-conflict",
        source_kind="raw_capture",
        source_uri="capture://permanent-conflict",
        content_hash="hash",
        canonical_subject="permanent-conflict",
        data_type="conversation_turn",
        producer="capture_service",
        intended_consumers=("capture_queue",),
        privacy_level="local",
        confidence=1.0,
        evidence_refs=("raw-revision:1",),
        dedupe_key="permanent-conflict",
        created_at=now_utc(),
    )
    ledger.record_data_event(event)
    ledger.record_data_consumed(
        event.event_id,
        consumer_id="capture_queue",
        outcome="first terminal outcome",
    )

    result = telemetry.cognitive_consumed(
        event.event_id,
        consumer_id="capture_queue",
        outcome="conflicting terminal outcome",
    )

    assert result is None
    assert not telemetry.outbox_path.exists()
    assert telemetry.dead_letter_path.is_file()
    dead_letters = telemetry.dead_letter_path.read_text(encoding="utf-8").splitlines()
    assert len(dead_letters) == 1
    assert "permanent_validation_failure" in dead_letters[0]


def test_outbox_quarantines_permanent_head_conflict_and_replays_valid_suffix(tmp_path):
    cfg = _config(tmp_path)
    ledger = ProducerConsumerLedger(cfg, initialize=True)
    telemetry = RuntimeFlowTelemetry(cfg)
    for suffix in ("conflict", "valid"):
        ledger.record_data_event(
            CognitiveDataEvent(
                event_id=f"cde-{suffix}",
                source_kind="raw_capture",
                source_uri=f"capture://{suffix}",
                content_hash=f"hash-{suffix}",
                canonical_subject=suffix,
                data_type="conversation_turn",
                producer="capture_service",
                intended_consumers=("capture_queue",),
                privacy_level="local",
                confidence=1.0,
                evidence_refs=(f"raw-revision:{suffix}",),
                dedupe_key=f"dedupe-{suffix}",
                created_at=now_utc(),
            )
        )
    ledger.record_data_consumed(
        "cde-conflict",
        consumer_id="capture_queue",
        outcome="existing",
    )
    telemetry._spool(  # noqa: SLF001 - exercise durable replay ordering
        {
            "operation": "cognitive_consumed",
            "event_id": "cde-conflict",
            "consumer_id": "capture_queue",
            "outcome": "conflict",
            "status": "consumed",
            "metadata": {},
        }
    )
    telemetry._spool(  # noqa: SLF001 - exercise durable replay ordering
        {
            "operation": "cognitive_consumed",
            "event_id": "cde-valid",
            "consumer_id": "capture_queue",
            "outcome": "valid",
            "status": "consumed",
            "metadata": {},
        }
    )

    replayed = telemetry.drain_outbox()

    assert replayed == 1
    assert not telemetry.outbox_path.exists()
    assert telemetry.dead_letter_path.is_file()
    snapshot = ledger.cognitive_data_snapshot()
    assert snapshot["counts"]["terminal_consumptions"] == 2


def test_wrapper_rejects_mock_database_paths_without_creating_artifacts(
    tmp_path, monkeypatch
):
    monkeypatch.chdir(tmp_path)
    config = MagicMock()
    config.database_dir = MagicMock()

    assert record_runtime_produced(
        "mock_flow",
        source="producer",
        item_id="item-1",
        intended_consumers=["consumer"],
        config_or_path=config,
    ) is None
    assert not (tmp_path / "MagicMock").exists()
