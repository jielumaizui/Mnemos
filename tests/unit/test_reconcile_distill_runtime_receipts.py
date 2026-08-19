from __future__ import annotations

import json
import sqlite3
import stat
from pathlib import Path
from types import SimpleNamespace

import pytest

from core.ops.cognitive_data_contract import CognitiveDataEvent
from core.ops.cognitive_pipeline_receipts import record_capture_worker_handoff
from core.ops.producer_consumer_ledger import DEFAULT_MATRIX, ProducerConsumerLedger
from scripts.reconcile_distill_runtime_receipts import (
    _canonical_sha256,
    reconcile_terminal_runtime_receipts,
)


def _config(tmp_path):
    return SimpleNamespace(database_dir=tmp_path)


def _create_queue_row(
    tmp_path,
    *,
    task_id: str = "task-1",
    session_id: str = "session-1",
    input_revision: str = "revision-1",
    status: str = "intentional_skip",
    cognitive_event_ids: list[str] | None = None,
    written_paths: list[str] | None = None,
    completed_at: str = "2026-07-13T00:01:00+00:00",
    terminal_reason: str = "typed terminal proof",
    retry_count: int = 0,
    max_retries: int = 3,
    progress_detail: str = "",
    create_table: bool = True,
) -> None:
    paths = written_paths or []
    queue_path = tmp_path / "distill_queue.db"
    with sqlite3.connect(queue_path) as conn:
        if create_table:
            conn.execute("""
                CREATE TABLE distillation_tasks (
                task_id TEXT PRIMARY KEY,
                session_id TEXT NOT NULL,
                input_revision TEXT NOT NULL,
                status TEXT NOT NULL,
                terminal_reason TEXT,
                written_count INTEGER NOT NULL,
                written_paths TEXT NOT NULL,
                proposal_ids TEXT NOT NULL DEFAULT '[]',
                required_consumer_receipts TEXT NOT NULL DEFAULT '[]',
                retry_count INTEGER NOT NULL DEFAULT 0,
                max_retries INTEGER NOT NULL DEFAULT 3,
                progress_detail TEXT NOT NULL DEFAULT '',
                meta TEXT,
                completed_at TEXT,
                updated_at TEXT
            )
            """)
        conn.execute(
            """
            INSERT INTO distillation_tasks (
                task_id, session_id, input_revision, status, terminal_reason,
                written_count, written_paths, proposal_ids,
                required_consumer_receipts, retry_count, max_retries,
                progress_detail, meta, completed_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, '[]', '[]', ?, ?, ?, ?, ?, ?)
            """,
            (
                task_id,
                session_id,
                input_revision,
                status,
                terminal_reason,
                len(paths),
                json.dumps(paths),
                retry_count,
                max_retries,
                progress_detail,
                json.dumps({"cognitive_sync_event_ids": cognitive_event_ids or []}),
                completed_at,
                completed_at,
            ),
        )


def _record_cognitive_event(
    ledger,
    *,
    event_id: str,
    session_id: str,
) -> None:
    ledger.record_data_event(
        CognitiveDataEvent(
            event_id=event_id,
            source_id=f"raw-{event_id}",
            asset_id=f"raw-{event_id}",
            source_kind="sync_engine",
            source_uri=f"sync://agent/{session_id}/turn/1",
            content_hash=f"content-{event_id}",
            canonical_subject=f"agent:{session_id}:turn:1",
            data_type="synced_turn",
            producer="sync_engine",
            intended_consumers=("amphora", "distill"),
            privacy_level="local",
            confidence=1.0,
            evidence_refs=(f"raw-{event_id}",),
            dedupe_key=f"sync-engine:{event_id}",
            created_at="2026-07-13T00:00:00+00:00",
        )
    )


def test_reconciler_applies_only_exact_typed_terminal_task_generation(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setattr(
        "scripts.reconcile_distill_runtime_receipts._runtime_writers_are_inactive",
        lambda _database_dir: True,
    )
    cfg = _config(tmp_path)
    ledger = ProducerConsumerLedger(cfg, initialize=True)
    ledger.register_adaptive_flows(DEFAULT_MATRIX)
    cognitive_event_id = "cde-reconcile-task"
    ledger.record_data_event(
        CognitiveDataEvent(
            event_id=cognitive_event_id,
            source_id="raw-event-1",
            asset_id="raw-event-1",
            source_kind="sync_engine",
            source_uri="sync://agent/session-1/turn/1",
            content_hash="content-hash-1",
            canonical_subject="agent:session-1:turn:1",
            data_type="synced_turn",
            producer="sync_engine",
            intended_consumers=("amphora", "distill"),
            privacy_level="local",
            confidence=1.0,
            evidence_refs=("raw-event-1",),
            dedupe_key="sync-engine:session-1:turn:1",
            created_at="2026-07-13T00:00:00+00:00",
        )
    )
    ledger.record_data_consumed(
        cognitive_event_id,
        consumer_id="amphora",
        outcome="distill_task_enqueued",
    )
    record_capture_worker_handoff(
        cfg,
        "session-1",
        SimpleNamespace(task_id="task-1", input_revision="revision-1"),
    )
    _create_queue_row(tmp_path, cognitive_event_ids=[cognitive_event_id])

    dry_run = reconcile_terminal_runtime_receipts(cfg, apply=False)
    applied = reconcile_terminal_runtime_receipts(
        cfg,
        apply=True,
        backup_dir=tmp_path / "backups",
        expected_plan_sha256=dry_run["plan_sha256"],
    )

    flow = ledger.snapshot()["flows"]["raw_quality_to_distill_gate"]
    assert dry_run["candidate_tasks"] == 1
    assert dry_run["unproven_by_reason"] == {}
    assert applied["ok"] is True
    assert applied["receipts_recorded"] == 1
    assert applied["handoff_tasks_reconciled"] == 1
    assert applied["terminal_cognitive_tasks_reconciled"] == 1
    assert applied["cognitive_receipts_deferred"] == 0
    assert applied["backup"]["ledger"]["integrity_check"] == "ok"
    assert applied["backup"]["queue"]["integrity_check"] == "ok"
    assert applied["terminal_outboxes_prepared"] == 1
    assert applied["terminal_outboxes_committed"] == 1
    assert flow["terminal_consumer_count"] == 1
    assert flow["pending_count"] == 0
    with sqlite3.connect(ledger.db_path) as conn:
        consumers = {
            str(row[0])
            for row in conn.execute(
                "SELECT consumer_id FROM cognitive_data_consumptions WHERE event_id = ?",
                (cognitive_event_id,),
            )
        }
    assert consumers == {"amphora", "distill"}
    assert not (tmp_path / "runtime_flow_outbox.jsonl").exists()


@pytest.mark.parametrize(
    ("legacy_metadata", "expected_supersession_reason"),
    [
        (
            {
                "transition": "value_prejudgment_completed",
                "verdict": "CERTAINLY_YES",
            },
            "legacy_prejudgment_false_terminal_replaced_by_verified_terminal_receipt",
        ),
        (
            {
                "transition": "distillation_terminal_receipt_verified",
                "receipt_status": "intentional_skip",
            },
            "legacy_terminal_missing_payload_binding_replaced_by_verified_terminal_receipt",
        ),
    ],
)
def test_reconciler_append_only_supersedes_exact_legacy_terminal(
    tmp_path,
    monkeypatch,
    legacy_metadata,
    expected_supersession_reason,
):
    from core.ops.cognitive_pipeline_receipts import (
        _active_runtime_terminal_receipts,
        find_runtime_terminal_receipts,
    )

    monkeypatch.setattr(
        "scripts.reconcile_distill_runtime_receipts._runtime_writers_are_inactive",
        lambda _database_dir: True,
    )
    cfg = _config(tmp_path)
    ledger = ProducerConsumerLedger(cfg, initialize=True)
    ledger.register_adaptive_flows(DEFAULT_MATRIX)
    record_capture_worker_handoff(
        cfg,
        "session-1",
        SimpleNamespace(task_id="task-1", input_revision="revision-1"),
    )
    _create_queue_row(tmp_path)
    with sqlite3.connect(ledger.db_path) as conn:
        event_id, generation_id, item_id = conn.execute(
            "SELECT event_id,generation_id,item_id FROM runtime_flow_events "
            "WHERE flow_id='raw_quality_to_distill_gate'"
        ).fetchone()
    legacy_receipt_id = ledger.record_consumed(
        "raw_quality_to_distill_gate",
        source="core/hephaestus/distillation_engine.py",
        item_id=item_id,
        production_event_id=event_id,
        generation_id=generation_id,
        metadata=legacy_metadata,
    )

    dry_run = reconcile_terminal_runtime_receipts(cfg, apply=False)
    replay = next(
        entry
        for entry in dry_run["reviewed_plan"]["entries"]
        if entry["disposition"] == "typed_terminal_outbox_replay"
    )
    applied = reconcile_terminal_runtime_receipts(
        cfg,
        apply=True,
        backup_dir=tmp_path / "backups",
        expected_plan_sha256=dry_run["plan_sha256"],
    )

    assert replay["runtime_terminal_action"] == "append_legacy_supersession"
    assert replay["supersedes_receipt_ids"] == [legacy_receipt_id]
    assert replay["supersession_reason"] == expected_supersession_reason
    assert applied["ok"] is True
    assert applied["terminal_outboxes_committed"] == 1
    terminals = find_runtime_terminal_receipts(
        ledger.db_path,
        "raw_quality_to_distill_gate",
        production_event_id=event_id,
    )
    active = _active_runtime_terminal_receipts(terminals)
    assert len(terminals) == 2
    assert len(active) == 1
    assert active[0]["metadata"]["transition"] == (
        "distillation_terminal_receipt_verified"
    )
    assert active[0]["metadata"]["supersedes_receipt_ids"] == [
        legacy_receipt_id
    ]
    assert (
        active[0]["metadata"]["supersession_reason"]
        == expected_supersession_reason
    )
    assert any(row["receipt_id"] == legacy_receipt_id for row in terminals)


def test_reconciler_never_supersedes_unclassified_terminal(tmp_path):
    cfg = _config(tmp_path)
    ledger = ProducerConsumerLedger(cfg, initialize=True)
    ledger.register_adaptive_flows(DEFAULT_MATRIX)
    record_capture_worker_handoff(
        cfg,
        "session-1",
        SimpleNamespace(task_id="task-1", input_revision="revision-1"),
    )
    _create_queue_row(tmp_path)
    with sqlite3.connect(ledger.db_path) as conn:
        event_id, generation_id, item_id = conn.execute(
            "SELECT event_id,generation_id,item_id FROM runtime_flow_events "
            "WHERE flow_id='raw_quality_to_distill_gate'"
        ).fetchone()
    legacy_receipt_id = ledger.record_consumed(
        "raw_quality_to_distill_gate",
        source="core/hephaestus/distillation_engine.py",
        item_id=item_id,
        production_event_id=event_id,
        generation_id=generation_id,
        metadata={"transition": "unknown_historical_terminal"},
    )

    dry_run = reconcile_terminal_runtime_receipts(cfg, apply=False)

    assert dry_run["candidate_tasks"] == 0
    assert dry_run["manual_reconciliation_required"] == 1
    assert dry_run["unproven_by_reason"] == {"terminal_receipt_conflict": 1}
    manual = next(
        entry
        for entry in dry_run["reviewed_plan"]["entries"]
        if entry["disposition"] == "manual:terminal_receipt_conflict"
    )
    assert manual["production_event_id"] == event_id
    with sqlite3.connect(ledger.db_path) as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM runtime_flow_receipts WHERE receipt_id=?",
            (legacy_receipt_id,),
        ).fetchone()[0] == 1


def test_reconciler_supersedes_legacy_cognitive_prejudgment_heads(
    tmp_path,
    monkeypatch,
):
    from core.ops.runtime_flow_lookup import cognitive_event_current_consumption

    monkeypatch.setattr(
        "scripts.reconcile_distill_runtime_receipts._runtime_writers_are_inactive",
        lambda _database_dir: True,
    )
    cfg = _config(tmp_path)
    ledger = ProducerConsumerLedger(cfg, initialize=True)
    ledger.register_adaptive_flows(DEFAULT_MATRIX)
    cognitive_event_id = "cde-legacy-prejudgment"
    _record_cognitive_event(
        ledger,
        event_id=cognitive_event_id,
        session_id="session-1",
    )
    old_amphora = ledger.record_data_consumed(
        cognitive_event_id,
        consumer_id="amphora",
        outcome="distill_task_enqueued",
    )
    old_distill = ledger.record_data_consumed(
        cognitive_event_id,
        consumer_id="distill",
        outcome="value_prejudgment_completed",
    )
    record_capture_worker_handoff(
        cfg,
        "session-1",
        SimpleNamespace(task_id="task-1", input_revision="revision-1"),
    )
    _create_queue_row(
        tmp_path,
        cognitive_event_ids=[cognitive_event_id],
    )
    with sqlite3.connect(ledger.db_path) as conn:
        event_id, generation_id, item_id = conn.execute(
            "SELECT event_id,generation_id,item_id FROM runtime_flow_events "
            "WHERE flow_id='raw_quality_to_distill_gate'"
        ).fetchone()
    old_runtime = ledger.record_consumed(
        "raw_quality_to_distill_gate",
        source="core/hephaestus/distillation_engine.py",
        item_id=item_id,
        production_event_id=event_id,
        generation_id=generation_id,
        metadata={
            "transition": "value_prejudgment_completed",
            "verdict": "CERTAINLY_YES",
        },
    )

    dry_run = reconcile_terminal_runtime_receipts(cfg, apply=False)
    replay = next(
        entry
        for entry in dry_run["reviewed_plan"]["entries"]
        if entry["disposition"] == "typed_terminal_outbox_replay"
    )
    applied = reconcile_terminal_runtime_receipts(
        cfg,
        apply=True,
        backup_dir=tmp_path / "backups",
        expected_plan_sha256=dry_run["plan_sha256"],
    )

    assert replay["supersedes_receipt_ids"] == [old_runtime]
    assert replay["cognitive_terminal_action"] == (
        "append_legacy_supersession"
    )
    assert set(replay["supersedes_cognitive_consumption_ids"]) == {
        old_amphora,
        old_distill,
    }
    assert applied["ok"] is True
    current_amphora = cognitive_event_current_consumption(
        ledger.db_path,
        cognitive_event_id,
        "amphora",
    )
    current_distill = cognitive_event_current_consumption(
        ledger.db_path,
        cognitive_event_id,
        "distill",
    )
    assert current_amphora["metadata"]["task_id"] == "task-1"
    assert current_amphora["supersedes_consumption_id"] == old_amphora
    assert current_distill["outcome"] == "distill_task_intentional_skip"
    assert current_distill["metadata"]["task_id"] == "task-1"
    assert current_distill["supersedes_consumption_id"] == old_distill


def test_reconciler_reopens_false_source_span_terminal_and_preserves_append_only_history(
    tmp_path,
    monkeypatch,
):
    from core.ops.cognitive_pipeline_receipts import (
        record_distillation_cognitive_terminal,
    )
    from core.ops.runtime_flow_lookup import cognitive_event_current_consumption
    from core.pipeline_receipts import DistillationWriteReceipt

    monkeypatch.setattr(
        "scripts.reconcile_distill_runtime_receipts._runtime_writers_are_inactive",
        lambda _database_dir: True,
    )
    cfg = _config(tmp_path)
    ledger = ProducerConsumerLedger(cfg, initialize=True)
    ledger.register_adaptive_flows(DEFAULT_MATRIX)
    cognitive_event_id = "cde-source-span-reopen"
    ledger.record_data_event(
        CognitiveDataEvent(
            event_id=cognitive_event_id,
            source_id="raw-event-source-span",
            asset_id="raw-event-source-span",
            source_kind="sync_engine",
            source_uri="sync://agent/session-1/turn/1",
            content_hash="content-hash-source-span",
            canonical_subject="agent:session-1:turn:1",
            data_type="synced_turn",
            producer="sync_engine",
            intended_consumers=("amphora", "distill"),
            privacy_level="local",
            confidence=1.0,
            evidence_refs=("raw-event-source-span",),
            dedupe_key="sync-engine:source-span-reopen",
            created_at="2026-07-13T00:00:00+00:00",
        )
    )
    ledger.record_data_consumed(
        cognitive_event_id,
        consumer_id="amphora",
        outcome="distill_task_enqueued",
    )
    record_capture_worker_handoff(
        cfg,
        "session-1",
        SimpleNamespace(task_id="task-1", input_revision="revision-1"),
    )
    _create_queue_row(
        tmp_path,
        cognitive_event_ids=[cognitive_event_id],
        terminal_reason="superseded_by_verified_source_span_migration:replacement-task",
    )
    with sqlite3.connect(tmp_path / "distill_queue.db") as conn:
        conn.execute(
            "CREATE TABLE amphora_source_span_migrations "
            "(legacy_task_id TEXT PRIMARY KEY, canonical_task_id TEXT NOT NULL)"
        )
        conn.execute(
            "INSERT INTO amphora_source_span_migrations VALUES ('task-1','replacement-task')"
        )
    with sqlite3.connect(ledger.db_path) as conn:
        event_id, generation_id, item_id = conn.execute(
            "SELECT event_id,generation_id,item_id FROM runtime_flow_events "
            "WHERE flow_id='raw_quality_to_distill_gate'"
        ).fetchone()
    wrong_runtime_receipt = ledger.record_consumed(
        "raw_quality_to_distill_gate",
        source="scripts/reconcile_amphora_source_spans.py",
        item_id=item_id,
        production_event_id=event_id,
        generation_id=generation_id,
        metadata={
            "transition": "verified_source_span_generation_superseded",
            "replacement_task_id": "replacement-task",
        },
    )
    wrong_cognitive_receipt = ledger.record_data_consumed(
        cognitive_event_id,
        consumer_id="distill",
        outcome="distill_task_intentional_skip",
        metadata={"task_id": "task-1"},
    )

    dry_run = reconcile_terminal_runtime_receipts(cfg, apply=False)
    applied = reconcile_terminal_runtime_receipts(
        cfg,
        apply=True,
        backup_dir=tmp_path / "backups",
        expected_plan_sha256=dry_run["plan_sha256"],
    )

    assert dry_run["source_span_superseded_tasks"] == 1
    assert dry_run["source_span_runtime_corrections_required"] == 1
    assert dry_run["source_span_cognitive_corrections_required"] == 1
    assert applied["ok"] is True
    assert applied["source_span_runtime_corrections_recorded"] == 1
    assert applied["source_span_cognitive_corrections_recorded"] == 1
    flow = ledger.snapshot()["flows"]["raw_quality_to_distill_gate"]
    assert flow["terminal_consumer_count"] == 1
    assert flow["pending_count"] == 0
    assert flow["extra_consumers"] == []
    reopened = cognitive_event_current_consumption(
        ledger.db_path,
        cognitive_event_id,
        "distill",
    )
    assert reopened is not None
    assert reopened["status"] == "revoked"
    assert reopened["metadata"]["reopen_required"] is True
    assert reopened["supersedes_consumption_id"] == wrong_cognitive_receipt
    assert ledger.snapshot()["cognitive_data"]["counts"]["missing_intended_consumptions"] == 1
    with sqlite3.connect(ledger.db_path) as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM runtime_flow_receipts WHERE receipt_id=?",
            (wrong_runtime_receipt,),
        ).fetchone()[0] == 1
        assert conn.execute(
            "SELECT COUNT(*) FROM cognitive_data_consumptions WHERE consumption_id=?",
            (wrong_cognitive_receipt,),
        ).fetchone()[0] == 1

    terminal = record_distillation_cognitive_terminal(
        cfg,
        task={
            "task_id": "replacement-task",
            "session_id": "session-1",
            "input_revision": "replacement-revision",
            "meta": {"cognitive_sync_event_ids": [cognitive_event_id]},
        },
        receipt=DistillationWriteReceipt(
            status="intentional_skip",
            terminal_reason="replacement reached a real terminal result",
        ),
    )

    assert terminal["cognitive_receipts"] == 1
    current = cognitive_event_current_consumption(
        ledger.db_path,
        cognitive_event_id,
        "distill",
    )
    assert current is not None
    assert current["status"] == "committed"
    assert current["outcome"] == "distill_task_intentional_skip"
    assert current["supersedes_consumption_id"] == reopened["consumption_id"]
    assert ledger.snapshot()["cognitive_data"]["counts"]["missing_intended_consumptions"] == 0


def test_reconciler_upgrades_legacy_source_span_successor_missing_reason(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setattr(
        "scripts.reconcile_distill_runtime_receipts._runtime_writers_are_inactive",
        lambda _database_dir: True,
    )
    cfg = _config(tmp_path)
    ledger = ProducerConsumerLedger(cfg, initialize=True)
    ledger.register_adaptive_flows(DEFAULT_MATRIX)
    record_capture_worker_handoff(
        cfg,
        "session-1",
        SimpleNamespace(task_id="task-1", input_revision="revision-1"),
    )
    _create_queue_row(
        tmp_path,
        terminal_reason="superseded_by_verified_source_span_migration:replacement-task",
    )
    with sqlite3.connect(tmp_path / "distill_queue.db") as conn:
        conn.execute(
            "CREATE TABLE amphora_source_span_migrations "
            "(legacy_task_id TEXT PRIMARY KEY, canonical_task_id TEXT NOT NULL)"
        )
        conn.execute(
            "INSERT INTO amphora_source_span_migrations VALUES "
            "('task-1','replacement-task')"
        )
    with sqlite3.connect(ledger.db_path) as conn:
        event_id, generation_id, item_id = conn.execute(
            "SELECT event_id,generation_id,item_id FROM runtime_flow_events "
            "WHERE flow_id='raw_quality_to_distill_gate'"
        ).fetchone()
    wrong_receipt = ledger.record_consumed(
        "raw_quality_to_distill_gate",
        source="scripts/reconcile_amphora_source_spans.py",
        item_id=item_id,
        production_event_id=event_id,
        generation_id=generation_id,
        metadata={
            "transition": "verified_source_span_generation_superseded",
            "replacement_task_id": "replacement-task",
        },
    )
    legacy_successor = ledger.record_skipped(
        "raw_quality_to_distill_gate",
        source="core/hephaestus/distillation_engine.py",
        item_id=item_id,
        production_event_id=event_id,
        generation_id=generation_id,
        metadata={
            "transition": "verified_source_span_generation_superseded",
            "replacement_task_id": "replacement-task",
            "recorded_by": "scripts/reconcile_amphora_source_spans.py",
            "supersedes_receipt_ids": [wrong_receipt],
        },
        idempotency_key=(
            "raw_quality_to_distill_gate:"
            f"{event_id}:source_span_generation_superseded:v2"
        ),
    )

    dry_run = reconcile_terminal_runtime_receipts(cfg, apply=False)

    assert dry_run["source_span_runtime_corrections_required"] == 1
    applied = reconcile_terminal_runtime_receipts(
        cfg,
        apply=True,
        backup_dir=tmp_path / "backups",
        expected_plan_sha256=dry_run["plan_sha256"],
    )
    assert applied["source_span_runtime_corrections_recorded"] == 1
    assert reconcile_terminal_runtime_receipts(cfg, apply=False)[
        "source_span_runtime_corrections_required"
    ] == 0
    flow = ledger.snapshot()["flows"]["raw_quality_to_distill_gate"]
    assert flow["missing_consumers"] == []
    assert flow["extra_consumers"] == []
    with sqlite3.connect(ledger.db_path) as conn:
        rows = conn.execute(
            "SELECT receipt_id, metadata FROM runtime_flow_receipts "
            "ORDER BY created_at, receipt_id"
        ).fetchall()
    assert len(rows) == 3
    successor_metadata = json.loads(rows[-1][1])
    assert successor_metadata["supersession_reason"] == (
        "source_span_generation_replaced_with_exact_raw"
    )
    assert set(successor_metadata["supersedes_receipt_ids"]) == {
        wrong_receipt,
        legacy_successor,
    }


@pytest.mark.parametrize(
    ("mutation_type", "current_name", "tombstone"),
    [
        ("delete", "later-removed.md", 1),
        ("move", "moved-destination.md", 0),
    ],
)
def test_reconciler_accepts_committed_page_removed_by_later_canonical_lifecycle(
    tmp_path,
    monkeypatch,
    mutation_type,
    current_name,
    tombstone,
):
    monkeypatch.setattr(
        "scripts.reconcile_distill_runtime_receipts._runtime_writers_are_inactive",
        lambda _database_dir: True,
    )
    cfg = _config(tmp_path)
    ledger = ProducerConsumerLedger(cfg, initialize=True)
    ledger.register_adaptive_flows(DEFAULT_MATRIX)
    record_capture_worker_handoff(
        cfg,
        "session-1",
        SimpleNamespace(task_id="task-1", input_revision="revision-1"),
    )
    removed_path = tmp_path / "wiki" / "later-removed.md"
    _create_queue_row(
        tmp_path,
        status="committed",
        written_paths=[str(removed_path)],
        completed_at="2026-07-12T06:39:00+00:00",
    )
    with sqlite3.connect(tmp_path / "wiki_projection.db") as conn:
        conn.execute("""
            CREATE TABLE wiki_mutations (
                mutation_id TEXT PRIMARY KEY,
                page_id TEXT NOT NULL,
                mutation_type TEXT NOT NULL,
                page_path TEXT NOT NULL,
                previous_path TEXT NOT NULL,
                tombstone INTEGER NOT NULL,
                created_at TEXT NOT NULL,
                sequence_no INTEGER NOT NULL UNIQUE
            )
            """)
        conn.executemany(
            "INSERT INTO wiki_mutations VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            [
                (
                    "mutation-create",
                    "page-1",
                    "create",
                    str(removed_path),
                    "",
                    0,
                    "2026-07-12T06:38:00+00:00",
                    1,
                ),
                (
                    "mutation-delete",
                    "page-1",
                    mutation_type,
                    str(removed_path.with_name(current_name)),
                    str(removed_path),
                    tombstone,
                    "2026-07-12T07:01:00+00:00",
                    2,
                ),
            ],
        )

    dry_run = reconcile_terminal_runtime_receipts(cfg, apply=False)
    applied = reconcile_terminal_runtime_receipts(
        cfg,
        apply=True,
        backup_dir=tmp_path / "backups",
        expected_plan_sha256=dry_run["plan_sha256"],
    )

    assert dry_run["candidate_tasks"] == 1
    assert dry_run["lifecycle_proven_terminal_tasks"] == 1
    assert dry_run["unproven_by_reason"] == {}
    assert applied["ok"] is True
    assert applied["receipts_recorded"] == 1
    assert applied["lifecycle_proven_terminal_tasks"] == 1


def test_reconciler_rejects_lifecycle_removal_that_predates_task_commit(
    tmp_path,
):
    cfg = _config(tmp_path)
    ledger = ProducerConsumerLedger(cfg, initialize=True)
    ledger.register_adaptive_flows(DEFAULT_MATRIX)
    record_capture_worker_handoff(
        cfg,
        "session-1",
        SimpleNamespace(task_id="task-1", input_revision="revision-1"),
    )
    removed_path = tmp_path / "wiki" / "removed-before-commit.md"
    _create_queue_row(
        tmp_path,
        status="committed",
        written_paths=[str(removed_path)],
        completed_at="2026-07-12T07:02:00+00:00",
    )
    with sqlite3.connect(tmp_path / "wiki_projection.db") as conn:
        conn.execute("""
            CREATE TABLE wiki_mutations (
                mutation_id TEXT PRIMARY KEY,
                page_id TEXT NOT NULL,
                mutation_type TEXT NOT NULL,
                page_path TEXT NOT NULL,
                previous_path TEXT NOT NULL,
                tombstone INTEGER NOT NULL,
                created_at TEXT NOT NULL,
                sequence_no INTEGER NOT NULL UNIQUE
            )
            """)
        conn.executemany(
            "INSERT INTO wiki_mutations VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            [
                (
                    "mutation-create",
                    "page-1",
                    "create",
                    str(removed_path),
                    "",
                    0,
                    "2026-07-12T06:38:00+00:00",
                    1,
                ),
                (
                    "mutation-delete",
                    "page-1",
                    "delete",
                    str(removed_path),
                    str(removed_path),
                    1,
                    "2026-07-12T07:01:00+00:00",
                    2,
                ),
            ],
        )

    dry_run = reconcile_terminal_runtime_receipts(cfg, apply=False)

    assert dry_run["candidate_tasks"] == 0
    assert dry_run["lifecycle_proven_terminal_tasks"] == 0
    assert dry_run["unproven_by_reason"] == {"committed_artifact_missing": 1}


def test_reconciler_requires_explicit_timezone_for_naive_legacy_completion(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setattr(
        "scripts.reconcile_distill_runtime_receipts._runtime_writers_are_inactive",
        lambda _database_dir: True,
    )
    cfg = _config(tmp_path)
    ledger = ProducerConsumerLedger(cfg, initialize=True)
    ledger.register_adaptive_flows(DEFAULT_MATRIX)
    record_capture_worker_handoff(
        cfg,
        "session-1",
        SimpleNamespace(task_id="task-1", input_revision="revision-1"),
    )
    removed_path = tmp_path / "wiki" / "removed-after-commit.md"
    _create_queue_row(
        tmp_path,
        status="committed",
        written_paths=[str(removed_path)],
        completed_at="2026-07-12T14:39:47.362283",
    )
    with sqlite3.connect(tmp_path / "wiki_projection.db") as conn:
        conn.execute("""
            CREATE TABLE wiki_mutations (
                mutation_id TEXT PRIMARY KEY,
                page_id TEXT NOT NULL,
                mutation_type TEXT NOT NULL,
                page_path TEXT NOT NULL,
                previous_path TEXT NOT NULL,
                tombstone INTEGER NOT NULL,
                created_at TEXT NOT NULL,
                sequence_no INTEGER NOT NULL UNIQUE
            )
            """)
        conn.executemany(
            "INSERT INTO wiki_mutations VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            [
                (
                    "mutation-create",
                    "page-1",
                    "create",
                    str(removed_path),
                    "",
                    0,
                    "2026-07-12T06:38:00+00:00",
                    1,
                ),
                (
                    "mutation-delete",
                    "page-1",
                    "delete",
                    str(removed_path),
                    str(removed_path),
                    1,
                    "2026-07-12T07:01:00+00:00",
                    2,
                ),
            ],
        )

    unspecified = reconcile_terminal_runtime_receipts(cfg, apply=False)
    reviewed = reconcile_terminal_runtime_receipts(
        cfg,
        apply=False,
        legacy_naive_timezone="Asia/Shanghai",
    )
    applied = reconcile_terminal_runtime_receipts(
        cfg,
        apply=True,
        backup_dir=tmp_path / "backups",
        expected_plan_sha256=reviewed["plan_sha256"],
        legacy_naive_timezone="Asia/Shanghai",
    )

    assert unspecified["candidate_tasks"] == 0
    assert unspecified["unproven_by_reason"] == {
        "committed_artifact_missing": 1
    }
    assert reviewed["candidate_tasks"] == 1
    assert reviewed["lifecycle_proven_terminal_tasks"] == 1
    assert reviewed["unproven_by_reason"] == {}
    assert reviewed["reviewed_plan"]["legacy_naive_timezone"] == "Asia/Shanghai"
    assert reviewed["plan_sha256"] != unspecified["plan_sha256"]
    assert applied["ok"] is True
    migration_receipt = json.loads(
        Path(applied["migration_receipt"]["path"]).read_text(encoding="utf-8")
    )
    assert migration_receipt["status"] == "completed"
    assert migration_receipt["legacy_naive_timezone"] == "Asia/Shanghai"


def test_reconciler_rejects_unknown_legacy_naive_timezone(tmp_path):
    cfg = _config(tmp_path)
    ProducerConsumerLedger(cfg, initialize=True)
    _create_queue_row(tmp_path)

    result = reconcile_terminal_runtime_receipts(
        cfg,
        apply=False,
        legacy_naive_timezone="Mars/Olympus_Mons",
    )

    assert result["ok"] is False
    assert result["error"] == "legacy_naive_timezone_invalid"


def test_reconciler_rejects_plan_drift_before_backup_or_write(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setattr(
        "scripts.reconcile_distill_runtime_receipts._runtime_writers_are_inactive",
        lambda _database_dir: True,
    )
    cfg = _config(tmp_path)
    ledger = ProducerConsumerLedger(cfg, initialize=True)
    ledger.register_adaptive_flows(DEFAULT_MATRIX)
    record_capture_worker_handoff(
        cfg,
        "session-1",
        SimpleNamespace(task_id="task-1", input_revision="revision-1"),
    )
    _create_queue_row(tmp_path)
    dry_run = reconcile_terminal_runtime_receipts(cfg, apply=False)
    assert len(dry_run["reviewed_plan"]["entries"]) == dry_run["plan_entries"]
    assert (
        _canonical_sha256(dry_run["reviewed_plan"])
        == dry_run["semantic_plan_sha256"]
    )
    assert dry_run["reviewed_plan"]["entries"] == [
        {
            "task_id": "task-1",
            "session_id": "session-1",
            "input_revision": "revision-1",
            "status": "intentional_skip",
            "production_event_id": dry_run["reviewed_plan"]["entries"][0][
                "production_event_id"
            ],
            "disposition": "typed_terminal_outbox_replay",
            "receipt_sha256": dry_run["reviewed_plan"]["entries"][0][
                "receipt_sha256"
            ],
            "cognitive_event_ids": [],
            "runtime_terminal_action": "append_new_terminal",
            "supersedes_receipt_ids": [],
            "supersession_reason": "",
            "cognitive_terminal_action": "not_applicable",
            "supersedes_cognitive_consumption_ids": [],
            "cognitive_supersession_reasons": [],
        }
    ]
    with sqlite3.connect(tmp_path / "distill_queue.db") as conn:
        conn.execute(
            "UPDATE distillation_tasks SET terminal_reason='drifted' "
            "WHERE task_id='task-1'"
        )

    rejected = reconcile_terminal_runtime_receipts(
        cfg,
        apply=True,
        backup_dir=tmp_path / "backups",
        expected_plan_sha256=dry_run["plan_sha256"],
    )

    assert rejected["ok"] is False
    assert rejected["error"] == "reviewed_plan_sha256_mismatch"
    assert not (tmp_path / "backups").exists()
    with sqlite3.connect(tmp_path / "distill_queue.db") as conn:
        meta = json.loads(
            conn.execute(
                "SELECT meta FROM distillation_tasks WHERE task_id='task-1'"
            ).fetchone()[0]
        )
        columns = {
            row[1]
            for row in conn.execute("PRAGMA table_info(distillation_tasks)")
        }
        trigger = conn.execute(
            "SELECT 1 FROM sqlite_master "
            "WHERE type='trigger' "
            "AND name='distillation_tasks_terminal_outbox_anchor_immutable'"
        ).fetchone()
    assert "terminal_receipt_outbox" not in meta
    assert "terminal_outbox_anchor_sha256" not in columns
    assert trigger is None


def test_reconciler_never_binds_ambiguous_legacy_cognitive_event(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setattr(
        "scripts.reconcile_distill_runtime_receipts._runtime_writers_are_inactive",
        lambda _database_dir: True,
    )
    cfg = _config(tmp_path)
    ledger = ProducerConsumerLedger(cfg, initialize=True)
    ledger.register_adaptive_flows(DEFAULT_MATRIX)
    event_id = "cde-ambiguous-legacy-handoff"
    _record_cognitive_event(
        ledger,
        event_id=event_id,
        session_id="shared",
    )
    original = ledger.record_data_consumed(
        event_id,
        consumer_id="amphora",
        outcome="distill_task_enqueued",
    )
    for index in range(2):
        task_id = f"task-{index}"
        session_id = f"session-{index}"
        revision = f"revision-{index}"
        record_capture_worker_handoff(
            cfg,
            session_id,
            SimpleNamespace(task_id=task_id, input_revision=revision),
        )
        _create_queue_row(
            tmp_path,
            task_id=task_id,
            session_id=session_id,
            input_revision=revision,
            cognitive_event_ids=[event_id],
            create_table=index == 0,
        )

    dry_run = reconcile_terminal_runtime_receipts(cfg, apply=False)
    applied = reconcile_terminal_runtime_receipts(
        cfg,
        apply=True,
        backup_dir=tmp_path / "backups",
        expected_plan_sha256=dry_run["plan_sha256"],
    )

    assert dry_run["unproven_by_reason"] == {
        "ambiguous_cognitive_event_task_mapping": 2
    }
    assert applied["ok"] is False
    assert applied["terminal_outboxes_prepared"] == 0
    with sqlite3.connect(ledger.db_path) as conn:
        rows = conn.execute(
            "SELECT consumption_id, metadata FROM cognitive_data_consumptions "
            "WHERE event_id=? AND consumer_id='amphora'",
            (event_id,),
        ).fetchall()
    assert len(rows) == 1
    assert rows[0][0] == original
    assert not json.loads(rows[0][1]).get("task_id")


def test_reconciler_failed_terminal_outbox_is_recoverable_idempotent_and_restorable(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setattr(
        "scripts.reconcile_distill_runtime_receipts._runtime_writers_are_inactive",
        lambda _database_dir: True,
    )
    cfg = _config(tmp_path)
    ledger = ProducerConsumerLedger(cfg, initialize=True)
    ledger.register_adaptive_flows(DEFAULT_MATRIX)
    record_capture_worker_handoff(
        cfg,
        "failed-session",
        SimpleNamespace(
            task_id="failed-task",
            input_revision="failed-revision",
        ),
    )
    _create_queue_row(
        tmp_path,
        task_id="failed-task",
        session_id="failed-session",
        input_revision="failed-revision",
        status="failed",
        terminal_reason="permanent failure",
        retry_count=3,
        max_retries=3,
        progress_detail="operator diagnostic may change",
    )
    dry_run = reconcile_terminal_runtime_receipts(cfg, apply=False)
    applied = reconcile_terminal_runtime_receipts(
        cfg,
        apply=True,
        backup_dir=tmp_path / "backups",
        expected_plan_sha256=dry_run["plan_sha256"],
    )

    assert applied["ok"] is True
    assert applied["failed_terminal_outboxes_prepared"] == 1
    assert applied["failed_terminal_outboxes_committed"] == 1
    assert applied["conservation"]["identity_and_status_conserved"] is True
    for backup in applied["backup"].values():
        backup_path = Path(backup["path"])
        assert stat.S_IMODE(backup_path.stat().st_mode) == 0o600
        assert stat.S_IMODE(backup_path.parent.stat().st_mode) == 0o700
    receipt_path = Path(applied["migration_receipt"]["path"])
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    receipt_sha256 = receipt.pop("receipt_sha256")
    assert receipt["status"] == "completed"
    assert receipt["reviewed_plan_sha256"] == dry_run["plan_sha256"]
    assert receipt["backup"] == applied["backup"]
    assert receipt["conservation"] == applied["conservation"]
    assert receipt["outcome"]["ok"] is True
    assert receipt_sha256 == _canonical_sha256(receipt)
    assert stat.S_IMODE(receipt_path.stat().st_mode) == 0o600
    with sqlite3.connect(tmp_path / "distill_queue.db") as conn:
        row = conn.execute(
            "SELECT meta, terminal_outbox_anchor_sha256 "
            "FROM distillation_tasks WHERE task_id='failed-task'"
        ).fetchone()
    outbox = json.loads(row[0])["failed_terminal_receipt_outbox"]
    assert outbox["status"] == "committed"
    assert outbox["reason"] == "retry_exhausted:permanent failure"
    assert row[1]
    with sqlite3.connect(tmp_path / "distill_queue.db") as conn:
        trigger = conn.execute(
            "SELECT sql FROM sqlite_master "
            "WHERE type='trigger' "
            "AND name='distillation_tasks_terminal_outbox_anchor_immutable'"
        ).fetchone()
        assert trigger is not None
        with pytest.raises(
            sqlite3.IntegrityError,
            match="terminal outbox anchor is immutable",
        ):
            conn.execute(
                "UPDATE distillation_tasks "
                "SET terminal_outbox_anchor_sha256=? "
                "WHERE task_id='failed-task'",
                ("f" * 64,),
            )

    second_dry_run = reconcile_terminal_runtime_receipts(cfg, apply=False)
    second = reconcile_terminal_runtime_receipts(
        cfg,
        apply=True,
        backup_dir=tmp_path / "backups-second",
        expected_plan_sha256=second_dry_run["plan_sha256"],
    )
    assert second["semantic_plan_sha256"] == dry_run["semantic_plan_sha256"]
    assert second["failed_terminal_outboxes_prepared"] == 0
    assert second["failed_terminal_outboxes_committed"] == 0
    assert second["receipts_recorded"] == 0

    restored = tmp_path / "restored-distill-queue.db"
    with sqlite3.connect(applied["backup"]["queue"]["path"]) as src:
        with sqlite3.connect(restored) as dst:
            src.backup(dst)
    with sqlite3.connect(restored) as conn:
        restored_meta = json.loads(
            conn.execute(
                "SELECT meta FROM distillation_tasks "
                "WHERE task_id='failed-task'"
            ).fetchone()[0]
        )
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    assert "failed_terminal_receipt_outbox" not in restored_meta


def test_multi_database_backup_failure_removes_only_this_unbound_backup_set(
    tmp_path,
    monkeypatch,
):
    from scripts import reconcile_distill_runtime_receipts as reconciler

    ledger = tmp_path / "ledger.db"
    queue = tmp_path / "queue.db"
    for path in (ledger, queue):
        with sqlite3.connect(path) as connection:
            connection.execute("CREATE TABLE sentinel (value TEXT NOT NULL)")
            connection.execute("INSERT INTO sentinel VALUES ('reviewed')")
    backup_dir = tmp_path / "backups"
    original = reconciler._backup_database  # noqa: SLF001
    calls = 0

    def fail_before_second_backup(source, target_dir, *, label):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise RuntimeError("injected second backup failure")
        return original(source, target_dir, label=label)

    monkeypatch.setattr(
        reconciler,
        "_backup_database",
        fail_before_second_backup,
    )

    with pytest.raises(RuntimeError, match="injected second backup failure"):
        reconciler._backup_database_set(  # noqa: SLF001
            (
                ("ledger", ledger, "producer-consumer"),
                ("queue", queue, "distill-queue"),
            ),
            backup_dir,
        )

    assert backup_dir.is_dir()
    assert list(backup_dir.iterdir()) == []


def test_terminal_anchor_schema_has_one_registered_ddl_owner():
    from scripts.generate_phase0_governance_contracts import _schema_inventory

    entries = {
        str(item["path"]): item
        for item in _schema_inventory()
    }
    amphora_owner = entries["core/kia/amphora.py"]
    assert amphora_owner["owner_status"] == "REGISTERED"
    assert amphora_owner["release_blocking"] is False
    assert "scripts/reconcile_distill_runtime_receipts.py" not in entries


def test_reconciler_corrupt_queue_meta_is_manual_and_never_overwritten(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setattr(
        "scripts.reconcile_distill_runtime_receipts._runtime_writers_are_inactive",
        lambda _database_dir: True,
    )
    cfg = _config(tmp_path)
    ledger = ProducerConsumerLedger(cfg, initialize=True)
    ledger.register_adaptive_flows(DEFAULT_MATRIX)
    record_capture_worker_handoff(
        cfg,
        "session-1",
        SimpleNamespace(task_id="task-1", input_revision="revision-1"),
    )
    _create_queue_row(tmp_path)
    with sqlite3.connect(tmp_path / "distill_queue.db") as conn:
        conn.execute(
            "UPDATE distillation_tasks SET meta='{broken' WHERE task_id='task-1'"
        )
    dry_run = reconcile_terminal_runtime_receipts(cfg, apply=False)
    applied = reconcile_terminal_runtime_receipts(
        cfg,
        apply=True,
        backup_dir=tmp_path / "backups",
        expected_plan_sha256=dry_run["plan_sha256"],
    )

    assert dry_run["unproven_by_reason"] == {
        "queue_terminal_json_invalid": 1
    }
    assert applied["ok"] is False
    assert applied["terminal_outboxes_prepared"] == 0
    with sqlite3.connect(tmp_path / "distill_queue.db") as conn:
        assert conn.execute(
            "SELECT meta FROM distillation_tasks WHERE task_id='task-1'"
        ).fetchone()[0] == "{broken"


def test_reconciler_never_synthesizes_unexhausted_failed_terminal(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setattr(
        "scripts.reconcile_distill_runtime_receipts._runtime_writers_are_inactive",
        lambda _database_dir: True,
    )
    cfg = _config(tmp_path)
    ledger = ProducerConsumerLedger(cfg, initialize=True)
    ledger.register_adaptive_flows(DEFAULT_MATRIX)
    record_capture_worker_handoff(
        cfg,
        "failed-session",
        SimpleNamespace(
            task_id="failed-task",
            input_revision="failed-revision",
        ),
    )
    _create_queue_row(
        tmp_path,
        task_id="failed-task",
        session_id="failed-session",
        input_revision="failed-revision",
        status="failed",
        terminal_reason="not exhausted",
        retry_count=1,
        max_retries=3,
    )
    dry_run = reconcile_terminal_runtime_receipts(cfg, apply=False)
    applied = reconcile_terminal_runtime_receipts(
        cfg,
        apply=True,
        backup_dir=tmp_path / "backups",
        expected_plan_sha256=dry_run["plan_sha256"],
    )

    assert dry_run["unproven_by_reason"] == {
        "failed_retry_budget_not_exhausted": 1
    }
    assert applied["failed_terminal_outboxes_prepared"] == 0
    with sqlite3.connect(tmp_path / "distill_queue.db") as conn:
        meta = json.loads(
            conn.execute(
                "SELECT meta FROM distillation_tasks "
                "WHERE task_id='failed-task'"
            ).fetchone()[0]
        )
    assert "failed_terminal_receipt_outbox" not in meta


def test_reconciler_resumes_after_crash_between_outbox_prepare_and_runtime_proof(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setattr(
        "scripts.reconcile_distill_runtime_receipts._runtime_writers_are_inactive",
        lambda _database_dir: True,
    )
    cfg = _config(tmp_path)
    ledger = ProducerConsumerLedger(cfg, initialize=True)
    ledger.register_adaptive_flows(DEFAULT_MATRIX)
    record_capture_worker_handoff(
        cfg,
        "session-1",
        SimpleNamespace(task_id="task-1", input_revision="revision-1"),
    )
    _create_queue_row(tmp_path)
    first_plan = reconcile_terminal_runtime_receipts(cfg, apply=False)
    from scripts import reconcile_distill_runtime_receipts as reconciler

    real_record_terminal = reconciler.record_distillation_terminal
    monkeypatch.setattr(
        reconciler,
        "record_distillation_terminal",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            SystemExit("injected_hard_crash_after_prepare")
        ),
    )
    with pytest.raises(SystemExit, match="injected_hard_crash_after_prepare"):
        reconcile_terminal_runtime_receipts(
            cfg,
            apply=True,
            backup_dir=tmp_path / "backups-first",
            expected_plan_sha256=first_plan["plan_sha256"],
        )
    interrupted_receipts = list(
        (tmp_path / "backups-first").glob(
            "distill-runtime-receipts-migration.*.json"
        )
    )
    assert len(interrupted_receipts) == 1
    interrupted_receipt = json.loads(
        interrupted_receipts[0].read_text(encoding="utf-8")
    )
    interrupted_sha256 = interrupted_receipt.pop("receipt_sha256")
    assert interrupted_receipt["status"] == "prepared"
    assert interrupted_sha256 == _canonical_sha256(interrupted_receipt)
    with sqlite3.connect(tmp_path / "distill_queue.db") as conn:
        prepared = json.loads(
            conn.execute(
                "SELECT meta FROM distillation_tasks WHERE task_id='task-1'"
            ).fetchone()[0]
        )["terminal_receipt_outbox"]
    assert prepared["status"] == "pending"

    monkeypatch.setattr(
        reconciler,
        "record_distillation_terminal",
        real_record_terminal,
    )
    recovery_plan = reconcile_terminal_runtime_receipts(cfg, apply=False)
    recovered = reconcile_terminal_runtime_receipts(
        cfg,
        apply=True,
        backup_dir=tmp_path / "backups-recovery",
        expected_plan_sha256=recovery_plan["plan_sha256"],
    )

    assert recovered["ok"] is True
    assert recovered["terminal_outboxes_prepared"] == 0
    assert recovered["terminal_outboxes_committed"] == 1


def test_reconciler_rolls_back_caught_failure_and_marks_receipt(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setattr(
        "scripts.reconcile_distill_runtime_receipts._runtime_writers_are_inactive",
        lambda _database_dir: True,
    )
    cfg = _config(tmp_path)
    ledger = ProducerConsumerLedger(cfg, initialize=True)
    ledger.register_adaptive_flows(DEFAULT_MATRIX)
    record_capture_worker_handoff(
        cfg,
        "session-1",
        SimpleNamespace(task_id="task-1", input_revision="revision-1"),
    )
    _create_queue_row(tmp_path)
    dry_run = reconcile_terminal_runtime_receipts(cfg, apply=False)
    before = ledger.snapshot()
    monkeypatch.setattr(
        "scripts.reconcile_distill_runtime_receipts.record_distillation_terminal",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            RuntimeError("injected_handled_failure")
        ),
    )

    failed = reconcile_terminal_runtime_receipts(
        cfg,
        apply=True,
        backup_dir=tmp_path / "backups",
        expected_plan_sha256=dry_run["plan_sha256"],
    )

    assert failed["ok"] is False
    assert failed["error"] == "RuntimeError"
    assert failed["rollback"]["verified"] is True
    assert ledger.snapshot() == before
    receipt = json.loads(
        Path(failed["migration_receipt"]["path"]).read_text(encoding="utf-8")
    )
    assert receipt["status"] == "rolled_back"
    assert receipt["outcome"]["ok"] is False
    assert receipt["rollback"]["verified"] is True
    with sqlite3.connect(tmp_path / "distill_queue.db") as conn:
        meta = json.loads(
            conn.execute(
                "SELECT meta FROM distillation_tasks WHERE task_id='task-1'"
            ).fetchone()[0]
        )
    assert "terminal_receipt_outbox" not in meta
