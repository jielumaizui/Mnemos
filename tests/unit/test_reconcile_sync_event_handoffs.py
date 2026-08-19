from __future__ import annotations

import json
import sqlite3
import zlib
from types import SimpleNamespace

import pytest

from core.ops.cognitive_data_contract import CognitiveDataEvent
from core.ops.producer_consumer_ledger import DEFAULT_MATRIX, ProducerConsumerLedger
from core.pipeline_receipts import DistillationEnqueueReceipt
from core.sync_framework.capture_schema import CaptureQueueSchema
from core.sync_framework.native_raw_contract_ledger import NativeRawContractLedger
from core.sync_framework.raw_event_store import RawEventStore
from scripts import reconcile_sync_event_handoffs
from scripts.reconcile_sync_event_handoffs import build_sync_event_handoff_replay_plan


def _config(tmp_path):
    return SimpleNamespace(database_dir=tmp_path)


def _record_sync_event(ledger: ProducerConsumerLedger, *, content_hash: str = "hash-1") -> None:
    ledger.record_data_event(
        CognitiveDataEvent(
            event_id="cde-sync-event",
            source_id="legacy-source-id",
            asset_id="legacy-asset-id",
            source_kind="sync_engine",
            source_uri="sync://codex/session-1/turn/7",
            content_hash=content_hash,
            canonical_subject="codex:session-1:turn:7",
            data_type="synced_turn",
            producer="sync_engine",
            intended_consumers=("amphora", "distill"),
            privacy_level="local",
            confidence=1.0,
            evidence_refs=("legacy-source-id",),
            dedupe_key="sync-engine:session-1:turn:7",
            created_at="2026-07-13T00:00:00+00:00",
        )
    )


def _create_queue(path) -> None:
    with sqlite3.connect(path) as conn:
        conn.execute("CREATE TABLE distillation_tasks (meta TEXT)")


def _create_raw_turn(path, *, event_id: str, revision_id: str, content_hash: str = "hash-1") -> None:
    with sqlite3.connect(path) as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS raw_turns (
                event_id TEXT PRIMARY KEY,
                current_revision_id TEXT,
                source_agent TEXT,
                session_id TEXT,
                turn_number INTEGER,
                content_hash TEXT,
                full_content_hash TEXT,
                completeness_status TEXT
            )
            """
        )
        conn.execute(
            "INSERT INTO raw_turns VALUES (?, ?, 'codex', 'session-1', 7, ?, '', 'complete')",
            (event_id, revision_id, content_hash),
        )


def _create_replayable_raw_turn(path) -> None:
    with sqlite3.connect(path) as conn:
        conn.execute(
            """
            CREATE TABLE raw_turns (
                event_id TEXT PRIMARY KEY,
                current_revision_id TEXT,
                source_agent TEXT,
                session_id TEXT,
                turn_number INTEGER,
                model_tag TEXT,
                conversation_at TEXT,
                source_path TEXT,
                source_files_json TEXT,
                content_hash TEXT,
                full_content_hash TEXT,
                completeness_status TEXT,
                completeness_json TEXT,
                metadata_json TEXT,
                tool_calls_json TEXT,
                tool_results_json TEXT,
                attachments_json TEXT,
                raw_event_refs_json TEXT,
                reasoning_blob BLOB,
                user_content_blob BLOB,
                assistant_content_blob BLOB,
                compression TEXT
            )
            """
        )
        conn.execute(
            """
            INSERT INTO raw_turns VALUES (?, ?, 'codex', 'session-1', 7,
                'model', '2026-07-13T00:00:00+00:00', '.', '[]', 'hash-1', '',
                'complete', '{}', '{}', '[]', '[]', '[]', '[]', ?, ?, ?, 'zlib')
            """,
            (
                "raw-logical",
                "rawrev-1",
                zlib.compress(b""),
                zlib.compress(b"question"),
                zlib.compress(b"answer"),
            ),
        )


def test_sync_handoff_replay_rejects_invalid_utf8_raw_bytes() -> None:
    with pytest.raises(UnicodeDecodeError):
        reconcile_sync_event_handoffs._decompress(  # noqa: SLF001
            zlib.compress(b"visible\xffhidden"),
            "zlib",
        )


def test_plan_requires_exact_raw_identity_and_content_hash(tmp_path) -> None:
    cfg = _config(tmp_path)
    ledger = ProducerConsumerLedger(cfg, initialize=True)
    _record_sync_event(ledger)
    _create_queue(tmp_path / "distill_queue.db")
    _create_raw_turn(tmp_path / "raw_events.db", event_id="raw-logical", revision_id="rawrev-1")

    plan = build_sync_event_handoff_replay_plan(cfg)

    assert plan["ok"] is True
    assert plan["eligible_sync_events"] == 1
    assert plan["replayable_events"] == 1
    assert plan["replayable_sessions"] == 1
    assert plan["blocked_by_reason"] == {}
    item = plan["groups"][0]["items"][0]
    assert item["raw_revision_id"] == "rawrev-1"
    assert item["cognitive_sync_event_ids"] == ["cde-sync-event"]


def test_plan_refuses_ambiguous_raw_content_hash_match(tmp_path) -> None:
    cfg = _config(tmp_path)
    ledger = ProducerConsumerLedger(cfg, initialize=True)
    _record_sync_event(ledger)
    _create_queue(tmp_path / "distill_queue.db")
    _create_raw_turn(tmp_path / "raw_events.db", event_id="raw-logical-a", revision_id="rawrev-a")
    _create_raw_turn(tmp_path / "raw_events.db", event_id="raw-logical-b", revision_id="rawrev-b")

    plan = build_sync_event_handoff_replay_plan(cfg)

    assert plan["ok"] is False
    assert plan["replayable_events"] == 0
    assert plan["blocked_by_reason"] == {"raw_content_hash_ambiguous": 1}


def test_plan_and_payload_bind_an_exact_historical_raw_revision(tmp_path) -> None:
    cfg = _config(tmp_path)
    ledger = ProducerConsumerLedger(cfg, initialize=True)
    _record_sync_event(ledger)
    _create_queue(tmp_path / "distill_queue.db")
    raw = RawEventStore(db_path=tmp_path / "raw_events.db")
    try:
        historical_revision = raw.upsert_turn(
            source_agent="codex",
            session_id="session-1",
            turn_number=7,
            user_content="historical question",
            assistant_content="historical answer",
            content_hash="hash-1",
            completeness={"visible_text": "full"},
        )
        current_revision = raw.upsert_turn(
            source_agent="codex",
            session_id="session-1",
            turn_number=7,
            user_content="current question",
            assistant_content="current answer",
            content_hash="hash-current",
            completeness={"visible_text": "full"},
        )
    finally:
        raw.close()

    plan = build_sync_event_handoff_replay_plan(cfg)
    item = plan["groups"][0]["items"][0]
    payload, content_hash = reconcile_sync_event_handoffs._raw_payload(
        tmp_path / "raw_events.db",
        item=item,
        reconciliation_id="reviewed-test-reconciliation",
        replay_generation=1,
    )

    assert plan["ok"] is True, plan["blocked_by_reason"]
    assert historical_revision != current_revision
    assert item["raw_revision_id"] == historical_revision
    assert payload["user_content"] == "historical question"
    assert payload["assistant_content"] == "historical answer"
    assert content_hash == "hash-1"


def test_plan_rejects_native_contract_quarantined_raw_revision(tmp_path) -> None:
    cfg = _config(tmp_path)
    ledger = ProducerConsumerLedger(cfg, initialize=True)
    _record_sync_event(ledger)
    _create_queue(tmp_path / "distill_queue.db")
    raw = RawEventStore(db_path=tmp_path / "raw_events.db")
    try:
        revision_id = raw.upsert_turn(
            source_agent="codex",
            session_id="session-1",
            turn_number=7,
            user_content="quarantined question",
            assistant_content="quarantined answer",
            content_hash="hash-1",
            completeness={"visible_text": "full"},
        )
        logical_event_id = raw.get_turn(revision_id)["logical_event_id"]
        connection = raw._pool.get_conn()  # noqa: SLF001
        NativeRawContractLedger().record_explicit(
            connection,
            logical_event_id=logical_event_id,
            revision_id=revision_id,
            support_manifest_hash="manifest",
            contract_state="nonconforming",
            contract_errors=["native_identity_ambiguous"],
            observed_at="2026-07-30T00:00:00+00:00",
        )
        connection.commit()
    finally:
        raw.close()

    plan = build_sync_event_handoff_replay_plan(cfg)

    assert plan["ok"] is False
    assert plan["replayable_events"] == 0
    assert plan["blocked_by_reason"] == {
        "raw_native_contract_not_admissible": 1,
    }


def test_payload_revalidates_native_contract_after_reviewed_plan(tmp_path) -> None:
    cfg = _config(tmp_path)
    ledger = ProducerConsumerLedger(cfg, initialize=True)
    _record_sync_event(ledger)
    _create_queue(tmp_path / "distill_queue.db")
    raw = RawEventStore(db_path=tmp_path / "raw_events.db")
    try:
        revision_id = raw.upsert_turn(
            source_agent="codex",
            session_id="session-1",
            turn_number=7,
            user_content="planned question",
            assistant_content="planned answer",
            content_hash="hash-1",
            completeness={"visible_text": "full"},
        )
        logical_event_id = raw.get_turn(revision_id)["logical_event_id"]
    finally:
        raw.close()
    plan = build_sync_event_handoff_replay_plan(cfg)
    item = plan["groups"][0]["items"][0]
    raw = RawEventStore(db_path=tmp_path / "raw_events.db")
    try:
        connection = raw._pool.get_conn()  # noqa: SLF001
        NativeRawContractLedger().record_explicit(
            connection,
            logical_event_id=logical_event_id,
            revision_id=revision_id,
            support_manifest_hash="manifest",
            contract_state="nonconforming",
            contract_errors=["native_identity_ambiguous"],
            observed_at="2026-07-30T00:01:00+00:00",
        )
        connection.commit()
    finally:
        raw.close()

    with pytest.raises(ValueError, match="raw_native_contract_not_admissible"):
        reconcile_sync_event_handoffs._raw_payload(
            tmp_path / "raw_events.db",
            item=item,
            reconciliation_id="reviewed-test-reconciliation",
            replay_generation=1,
        )


def test_apply_replays_exact_raw_handoff_without_invoking_a_model(tmp_path, monkeypatch) -> None:
    cfg = _config(tmp_path)
    ledger = ProducerConsumerLedger(cfg, initialize=True)
    ledger.register_adaptive_flows(DEFAULT_MATRIX)
    _record_sync_event(ledger)
    _create_replayable_raw_turn(tmp_path / "raw_events.db")
    CaptureQueueSchema.initialize(tmp_path / "capture_queue.db")
    sqlite3.connect(tmp_path / "distill_queue.db").close()

    monkeypatch.setattr(
        reconcile_sync_event_handoffs,
        "_runtime_writers_are_inactive",
        lambda _database_dir: True,
    )
    calls = []

    def _enqueue_with_receipt(**kwargs):
        calls.append(kwargs)
        with sqlite3.connect(tmp_path / "distill_queue.db") as conn:
            conn.execute("CREATE TABLE IF NOT EXISTS distillation_tasks (meta TEXT)")
            conn.execute(
                "INSERT INTO distillation_tasks(meta) VALUES (?)",
                (json.dumps(kwargs["meta"]),),
            )
        return DistillationEnqueueReceipt(
            receipt_id="amphora-task-1",
            task_id="task-1",
            source_agent="codex",
            session_id="session-1",
            input_revision=kwargs["meta"]["input_revision"],
            status="pending",
            created=True,
        )

    monkeypatch.setattr("core.kia.amphora.enqueue_with_receipt", _enqueue_with_receipt)
    reviewed = build_sync_event_handoff_replay_plan(cfg)

    result = reconcile_sync_event_handoffs.reconcile_sync_event_handoffs(
        cfg,
        apply=True,
        backup_dir=tmp_path / "backups",
        expected_inventory_hash=reviewed["inventory_hash"],
    )

    assert result["ok"] is True
    assert result["applied"]["queue_events_created"] == 1
    assert result["applied"]["handoffs_committed"] == 1
    assert result["applied"]["task_receipts_created"] == 1
    assert result["status"] == "verified"
    assert len(calls) == 1
    assert len(result["backup"]) == 3
    with sqlite3.connect(tmp_path / "capture_queue.db") as conn:
        status = conn.execute(
            "SELECT status FROM capture_distillation_handoffs"
        ).fetchone()[0]
    assert status == "committed"
    with sqlite3.connect(ledger.db_path) as conn:
        consumers = {
            str(row[0])
            for row in conn.execute(
                "SELECT consumer_id FROM cognitive_data_consumptions WHERE event_id='cde-sync-event'"
            )
        }
    assert consumers == {"amphora"}


def test_apply_failure_restores_the_reviewed_inventory(tmp_path, monkeypatch) -> None:
    cfg = _config(tmp_path)
    ledger = ProducerConsumerLedger(cfg, initialize=True)
    ledger.register_adaptive_flows(DEFAULT_MATRIX)
    _record_sync_event(ledger)
    _create_replayable_raw_turn(tmp_path / "raw_events.db")
    CaptureQueueSchema.initialize(tmp_path / "capture_queue.db")
    sqlite3.connect(tmp_path / "distill_queue.db").close()
    reviewed = build_sync_event_handoff_replay_plan(cfg)
    monkeypatch.setattr(
        reconcile_sync_event_handoffs,
        "_runtime_writers_are_inactive",
        lambda _database_dir: True,
    )

    def _fail_enqueue(**_kwargs):
        raise RuntimeError("injected handoff failure")

    monkeypatch.setattr("core.kia.amphora.enqueue_with_receipt", _fail_enqueue)

    result = reconcile_sync_event_handoffs.reconcile_sync_event_handoffs(
        cfg,
        apply=True,
        backup_dir=tmp_path / "backups",
        expected_inventory_hash=reviewed["inventory_hash"],
    )

    assert result["status"] == "rolled_back", result
    assert result["rollback_verified"] is True
    rolled_back = build_sync_event_handoff_replay_plan(cfg)
    assert rolled_back["inventory_hash"] == reviewed["inventory_hash"]
    with sqlite3.connect(tmp_path / "capture_queue.db") as conn:
        assert int(conn.execute("SELECT COUNT(*) FROM capture_events").fetchone()[0]) == 0


def test_restore_replace_failure_preserves_existing_database_and_cleans_stage(
    tmp_path,
    monkeypatch,
) -> None:
    source = tmp_path / "source.db"
    with sqlite3.connect(source) as connection:
        connection.execute("CREATE TABLE sentinel (value TEXT NOT NULL)")
        connection.execute("INSERT INTO sentinel VALUES ('reviewed')")
    backup = reconcile_sync_event_handoffs._backup_databases(  # noqa: SLF001
        [source],
        tmp_path / "backups",
    )[0]
    with sqlite3.connect(source) as connection:
        connection.execute("UPDATE sentinel SET value='current'")
    before = source.read_bytes()

    def fail_replace(_source, _target):
        raise OSError("injected atomic replace failure")

    monkeypatch.setattr(
        reconcile_sync_event_handoffs.os,
        "replace",
        fail_replace,
    )

    with pytest.raises(OSError, match="injected atomic replace failure"):
        reconcile_sync_event_handoffs._restore_databases([backup])  # noqa: SLF001

    assert source.read_bytes() == before
    with sqlite3.connect(source) as connection:
        assert connection.execute("SELECT value FROM sentinel").fetchone() == (
            "current",
        )
    assert not [
        item.name
        for item in tmp_path.iterdir()
        if ".restore" in item.name
    ]
