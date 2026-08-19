from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import Mock

from core.app.context_search import ContextAwareSearch
from core.hephaestus.distillation_engine import DistillationEngine
from core.hephaestus.distillation_prejudge import ValuePrejudgment
from core.ops.producer_consumer_ledger import ProducerConsumerLedger
from core.ops.runtime_flow_health import (
    bootstrap_runtime_producer_consumer_ledger,
)
from core.scoring.adaptive_scorer_v2 import AdaptiveScorerV2
from core.pipeline_receipts import DistillationEnqueueReceipt
from core.sync_framework.agent_source import AgentSource, SessionInfo, Turn
from core.sync_framework.capture_queue import CaptureQueue
from core.sync_framework.capture_schema import CaptureQueueSchema
from core.sync_framework.capture_service import CaptureService
from core.sync_framework.capture_worker import CaptureWorkerPool


def _config(tmp_path):
    values = {
        "capture.max_workers": 1,
        "capture.per_source_concurrency": 1,
        "capture.max_batch_per_tick": 10,
        "capture.tick_interval_seconds": 0.01,
        "raw_event_store.enabled": True,
        "distill.auto": True,
    }
    return SimpleNamespace(
        data_dir=tmp_path,
        database_dir=tmp_path,
        wiki_dir=tmp_path / "wiki",
        obsidian_vault_path=tmp_path / "raw",
        get=lambda key, default=None: values.get(key, default),
    )


class _PipelineSource(AgentSource):
    name = "codex"
    model_tag = "codex"

    def discover_sessions(self):
        return []

    def parse_turns(self, session_path):
        return []


def test_real_search_feedback_path_writes_automatic_runtime_receipts(tmp_path) -> None:
    from core.access_policy import AccessNarrowing, PrincipalEnvelope
    from core.cognitive.access_control import make_cognitive_access_envelope
    from core.scoring.subject_provenance import record_scoring_subject_provenance

    cfg = _config(tmp_path)
    bootstrap_runtime_producer_consumer_ledger(cfg)
    db_path = tmp_path / "mnemos.db"
    AdaptiveScorerV2.ensure_tables(str(db_path))
    with sqlite3.connect(db_path) as conn:
        cursor = conn.execute(
            """
            INSERT INTO search_sessions (
                session_id, query, result_paths, created_at, outcome_status
            ) VALUES (?, ?, ?, ?, '')
            """,
            (
                "search-real-pipeline",
                "runtime evidence",
                json.dumps(["page.md"]),
                datetime.now().isoformat(),
            ),
        )
        principal = PrincipalEnvelope(
            principal_id="test:runtime-search",
            agent="codex",
            host_kind="test",
            capability_id="runtime-search",
            capabilities=frozenset({"memory_read", "memory_write"}),
            allowed_projects=frozenset({"mnemos"}),
        )
        narrowing = AccessNarrowing(
            session_id="runtime-search-session",
            project="mnemos",
        )
        access_control = make_cognitive_access_envelope(
            owner_principal_id=principal.principal_id,
            owner_agent=principal.agent,
            scope_type="session",
            scope_id=narrowing.session_id,
            session_id=narrowing.session_id,
            project=narrowing.project,
            purposes=(
                "cognitive_state_read",
                "cognitive_state_write",
                "score_training",
                "search_feedback",
            ),
            consent_provenance_refs=("runtime-search-consent",),
            sensitivity="sensitive",
            retention_policy="test",
            source_acl_lineage=("sha256:" + "b" * 64,),
            visibility="private",
        )
        record_scoring_subject_provenance(
            conn,
            object_type="search_session",
            object_id=str(cursor.lastrowid),
            subject_provenance=access_control,
        )

    feedback_result = ContextAwareSearch.record_search_click(
        "page.md",
        db_path=db_path,
        principal=principal,
        narrowing=narrowing,
    )
    assert feedback_result["success"] is True
    assert feedback_result["terminal_receipt_count"] == 7

    from core.cognitive.state_store import CognitiveStateStore

    state = CognitiveStateStore(tmp_path / "producer_consumer_ledger.db")
    assert len(state.current_revisions(object_type="user_reaction_event")) == 1
    assert len(state.current_revisions(object_type="feedback_attribution_record")) == 1
    with sqlite3.connect(db_path) as conn:
        tables = {
            str(row[0])
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        }
    assert "ground_truth_signals" not in tables
    assert "scorer_training_queue" not in tables


def test_real_capture_queue_worker_path_writes_cognitive_receipts(tmp_path, monkeypatch) -> None:
    cfg = _config(tmp_path)
    bootstrap_runtime_producer_consumer_ledger(cfg)
    queue_path = tmp_path / "capture_queue.db"
    CaptureQueueSchema.initialize(queue_path)
    queue = CaptureQueue(db_path=str(queue_path))
    engine = Mock()
    engine.config = cfg
    engine.sync_single_turn.return_value = SimpleNamespace(action="new", error="")
    worker_stub = SimpleNamespace(engine=engine, _running=False, start=lambda: None)
    CaptureService.reset_instance()
    monkeypatch.setattr("core.sync_framework.capture_service.get_config", lambda: cfg)
    service = CaptureService(
        queue=queue,
        worker_pool=worker_stub,
        start_worker=False,
    )

    result = service.capture_turn(
        source_agent="codex",
        session_id="session-real-pipeline",
        turn_number=0,
        user_content="Explain the runtime receipt design",
        assistant_content="Every intended consumer records a terminal outcome.",
    )

    assert result["status"] == "queued"
    before_worker = ProducerConsumerLedger(cfg, initialize=True).cognitive_data_snapshot()
    assert before_worker["counts"]["events"] == 2
    assert before_worker["counts"]["missing_intended_consumptions"] == 1

    event = queue.dequeue_by_session("codex", "session-real-pipeline")[0]
    worker = CaptureWorkerPool(queue=queue, sync_engine=engine)
    worker._process_event(event, source=Mock(name="codex-source"))

    after_worker = ProducerConsumerLedger(cfg, initialize=True).cognitive_data_snapshot()
    assert after_worker["status"] == "ok"
    assert after_worker["counts"]["events"] == 2
    assert after_worker["counts"]["intended_consumptions"] == 2
    assert after_worker["counts"]["terminal_consumptions"] == 2
    handoff = queue.create_distillation_handoff(
        "codex", "session-real-pipeline", [event], enabled=True
    )
    receipt = DistillationEnqueueReceipt(
        receipt_id="worker-receipt",
        task_id="worker-task",
        source_agent="codex",
        session_id="session-real-pipeline",
        input_revision=handoff["input_revision"],
        status="queued",
        created=True,
    )
    monkeypatch.setattr("core.kia.amphora.enqueue_with_receipt", lambda **kwargs: receipt)
    monkeypatch.setattr(worker, "_record_handoff_provenance", lambda meta, task_id: None)
    assert worker._dispatch_handoff(handoff) is True
    raw_flow = ProducerConsumerLedger(cfg, initialize=True).snapshot()["flows"][
        "raw_quality_to_distill_gate"
    ]
    assert raw_flow["produced_count"] == 1
    assert raw_flow["pending_count"] == 1
    CaptureService.reset_instance()
    worker.close()
    queue.close()


def test_real_sync_handoff_and_distill_boundary_close_all_cognitive_receipts(
    tmp_path, monkeypatch
) -> None:
    cfg = _config(tmp_path)
    cfg.get = lambda key, default=None: {
        "raw_event_store.enabled": False,
        "raw_projection.enabled": False,
    }.get(key, default)
    bootstrap_runtime_producer_consumer_ledger(cfg)
    backend = Mock()
    backend.save.return_value = [SimpleNamespace(uid="memory-1")]
    backend.list_by_tags.return_value = []

    from core.sync_framework.sync_engine import SyncEngine

    engine = SyncEngine(
        backend=backend,
        db_path=str(tmp_path / "sync_log.db"),
        config=cfg,
    )
    source = _PipelineSource()
    session = SessionInfo(
        session_id="session-sync-distill-real",
        source_path=tmp_path / "session.jsonl",
    )
    turn = Turn(
        turn_number=0,
        user_content="Preserve producer consumer evidence",
        assistant_content="Every intended consumer emits a terminal receipt.",
    )
    results = engine.sync_turns(
        source,
        session,
        [turn],
        incremental=False,
        enqueue_distillation=False,
    )
    assert results[0].action == "new"
    sync_event_id = str(turn.metadata["cognitive_sync_event_id"])

    receipt = DistillationEnqueueReceipt(
        receipt_id="receipt-1",
        task_id="task-1",
        source_agent="codex",
        session_id=session.session_id,
        input_revision="revision-1",
        status="queued",
        created=True,
    )
    monkeypatch.setattr("core.kia.amphora.enqueue_with_receipt", lambda **kwargs: receipt)
    engine.enqueue_session_for_distillation(source, session, [turn])

    distiller = object.__new__(DistillationEngine)
    distiller._runtime_receipt_config = cfg
    monkeypatch.setattr(distiller, "_check_paused", lambda result: False)
    monkeypatch.setattr(distiller, "_run_noise_filter", lambda result, messages: messages)
    monkeypatch.setattr(
        distiller,
        "_run_value_prejudgment",
        lambda result, messages: (ValuePrejudgment.CERTAINLY_NO, 1.0),
    )
    monkeypatch.setattr("core.hephaestus.distillation_engine.get_config", lambda: cfg)
    result = distiller.process(
        session.session_id,
        [{"role": "user", "content": turn.user_content}],
        meta={
            "_amphora_task_id": receipt.task_id,
            "input_revision": receipt.input_revision,
            "cognitive_sync_event_ids": [sync_event_id],
        },
    )

    # The CERTAINLY_NO prejudgment is a nonterminal stage event: persona and
    # amphora receipts are closed, but the distill generation stays pending
    # until the typed write receipt reaches a terminal classification.
    snapshot = ProducerConsumerLedger(cfg, initialize=True).snapshot()
    assert snapshot["cognitive_data"]["counts"]["terminal_consumptions"] == 2
    flow = snapshot["flows"]["raw_quality_to_distill_gate"]
    assert flow["terminal_consumer_count"] == 0
    assert flow["pending_count"] == 1

    from core.hephaestus.distillation_write_receipt import persist_with_receipt
    from core.ops.cognitive_pipeline_receipts import record_distillation_terminal

    write_receipt = persist_with_receipt(distiller, result, cfg)
    assert write_receipt.status == "intentional_skip"
    evidence = record_distillation_terminal(
        cfg,
        task={
            "task_id": receipt.task_id,
            "session_id": session.session_id,
            "input_revision": receipt.input_revision,
            "meta": {"cognitive_sync_event_ids": [sync_event_id]},
        },
        receipt=write_receipt,
    )

    snapshot = ProducerConsumerLedger(cfg, initialize=True).snapshot()
    assert evidence["matched"] is True
    assert snapshot["cognitive_data"]["status"] == "ok"
    assert snapshot["cognitive_data"]["counts"]["intended_consumptions"] == 3
    assert snapshot["cognitive_data"]["counts"]["terminal_consumptions"] == 3
    flow = snapshot["flows"]["raw_quality_to_distill_gate"]
    assert flow["observation_state"] == "observed"
    assert flow["terminal_consumer_count"] == 1
    assert flow["pending_count"] == 0
    engine.close()
