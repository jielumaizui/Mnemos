from __future__ import annotations

import time
from pathlib import Path

import pytest

from core.mnemos_bus import Event, EventBus, HandlerOutcome
from core.wiki_projection_lifecycle import (
    WikiProjectionLedger,
    projection_snapshot_hash,
)
from core.wiki_projection_publisher import publish_unpublished_mutations
from core.wiki_projection_publisher import publish_wiki_mutation


def _wait_for(predicate, timeout: float = 3.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.02)
    raise AssertionError("timed out waiting for EventBus state")


@pytest.fixture
def bus(tmp_path, monkeypatch):
    monkeypatch.setattr(EventBus, "_recover_pending", lambda self: None)
    instance = EventBus(root_dir=tmp_path)
    instance.close()
    instance._db_path = tmp_path / "events.db"
    instance._projection_db_path = tmp_path / "wiki_projection.db"
    instance._init_db()
    instance._max_retries = 3
    instance._retry_base_seconds = 0
    instance._retry_max_seconds = 0
    instance.start_dispatch()
    monkeypatch.setattr("core.mnemos_bus._global_bus", instance)
    monkeypatch.setattr(
        "core.mnemos_bus.publish_event",
        lambda event_type, agent, payload, *, trace_id="", subject_provenance=None: instance.publish(
            Event(
                event_type=event_type,
                source=agent,
                payload=payload,
                trace_id=trace_id,
                subject_provenance=subject_provenance,
            )
        ),
    )
    monkeypatch.setattr(
        "core.wiki_projection_lifecycle._default_db_path",
        lambda: tmp_path / "wiki_projection.db",
    )
    yield instance
    instance.stop_dispatch()
    instance.close()


def _event_status(bus: EventBus, trace_id: str) -> str:
    row = bus._get_conn().execute(
        "SELECT status FROM events WHERE trace_id=?", (trace_id,)
    ).fetchone()
    return str(row["status"]) if row else ""


def test_false_and_soft_error_are_retryable_not_acknowledged(bus):
    calls = {"false": 0, "dict": 0}

    def false_handler(_event):
        calls["false"] += 1
        return False

    def dict_handler(_event):
        calls["dict"] += 1
        return {"status": "error", "error": "projection unavailable"}

    bus.subscribe("soft_failure", false_handler)
    bus.subscribe("soft_failure", dict_handler)
    event = Event("soft_failure", "test", {})
    bus.publish(event)

    def dead_lettered() -> bool:
        row = bus._get_conn().execute(
            "SELECT 1 FROM dead_letters WHERE trace_id=?", (event.trace_id,)
        ).fetchone()
        return row is not None

    _wait_for(dead_lettered)
    conn = bus._get_conn()
    receipts = conn.execute(
        "SELECT disposition FROM handler_receipts WHERE trace_id=?",
        (event.trace_id,),
    ).fetchall()
    assert calls == {"false": 3, "dict": 3}
    assert {row["disposition"] for row in receipts} == {"retry"}
    assert conn.execute(
        "SELECT 1 FROM events WHERE trace_id=?", (event.trace_id,)
    ).fetchone() is None


def test_typed_noop_is_a_durable_success(bus):
    bus.subscribe("typed_noop", lambda _event: HandlerOutcome.noop("index", "not applicable"))
    event = Event("typed_noop", "test", {})
    bus.publish(event)
    _wait_for(lambda: _event_status(bus, event.trace_id) == "done")
    receipt = bus._get_conn().execute(
        "SELECT consumer, disposition FROM handler_receipts WHERE trace_id=?",
        (event.trace_id,),
    ).fetchone()
    assert dict(receipt) == {"consumer": "index", "disposition": "noop"}


def test_retry_tracking_keeps_distinct_handlers_with_the_same_display_name(bus):
    calls = {"first": 0, "second": 0}

    def first(_event):
        calls["first"] += 1

    def second(_event):
        calls["second"] += 1
        if calls["second"] == 1:
            return HandlerOutcome.retry("second_projection", "retry once")
        return HandlerOutcome.ack("second_projection")

    first.__name__ = second.__name__ = "projection_consumer"
    bus.subscribe("same_display_name", first)
    bus.subscribe("same_display_name", second)
    event = Event("same_display_name", "test", {})
    bus.publish(event)

    _wait_for(lambda: _event_status(bus, event.trace_id) == "done")
    assert calls == {"first": 1, "second": 2}


def test_republishing_the_same_trace_id_is_idempotent(bus):
    calls = {"count": 0}

    def handler(_event):
        calls["count"] += 1

    bus.subscribe("idempotent_publish", handler)
    first = Event("idempotent_publish", "test", {}, trace_id="stable-mutation-trace")
    duplicate = Event("idempotent_publish", "test", {}, trace_id="stable-mutation-trace")

    assert bus.publish(first) == "stable-mutation-trace"
    assert bus.publish(duplicate) == "stable-mutation-trace"
    _wait_for(lambda: _event_status(bus, first.trace_id) == "done")

    assert calls == {"count": 1}
    event_count = bus._get_conn().execute(
        "SELECT COUNT(*) FROM events WHERE trace_id=?", (first.trace_id,)
    ).fetchone()[0]
    assert event_count == 1


def test_unpublished_mutation_uses_mutation_id_as_stable_event_trace(monkeypatch, tmp_path):
    ledger = WikiProjectionLedger(tmp_path / "projection.db")
    page = tmp_path / "vault" / "page.md"
    page.parent.mkdir()
    page.write_text("# Stable event identity\n", encoding="utf-8")
    mutation = ledger.record_mutation(page, mutation_type="create")
    published = []

    def fake_publish(
        event_type, source, payload, *, trace_id="", subject_provenance=None
    ):
        published.append((event_type, source, payload, trace_id))
        return trace_id

    monkeypatch.setattr("core.mnemos_bus.publish_event", fake_publish)
    result = publish_unpublished_mutations(ledger)

    assert result["published"] == 1
    assert published[0][3] == mutation.mutation_id
    assert ledger.unpublished_mutations() == []


def test_publish_wiki_mutation_uses_the_injected_bus_and_matching_ledger(tmp_path):
    """A subject delete must not escape to the process-global EventBus."""

    ledger = WikiProjectionLedger(tmp_path / "projection.db")
    page = tmp_path / "vault" / "page.md"
    page.parent.mkdir()
    page.write_text("# Injected projection owner\n", encoding="utf-8")
    mutation = ledger.record_mutation(page, mutation_type="create")
    published = []

    class Bus:
        def publish(self, event):
            published.append(event)
            return event.trace_id

    result = publish_wiki_mutation(mutation, ledger=ledger, event_bus=Bus())

    assert result["event_trace_id"] == mutation.mutation_id
    assert len(published) == 1
    assert published[0].trace_id == mutation.mutation_id
    assert published[0].payload["mutation_id"] == mutation.mutation_id
    assert published[0].subject_provenance is not None
    assert published[0].subject_provenance["visibility"] == "system"
    assert ledger.mutation_receipt(mutation.mutation_id).event_trace_id == mutation.mutation_id


def test_producer_event_trace_is_immutable(tmp_path):
    ledger = WikiProjectionLedger(tmp_path / "projection.db")
    page = tmp_path / "vault" / "page.md"
    page.parent.mkdir()
    page.write_text("# Stable producer provenance\n", encoding="utf-8")
    mutation = ledger.record_mutation(page, mutation_type="create")

    ledger.attach_event(mutation.mutation_id, "producer-trace")
    ledger.attach_event(mutation.mutation_id, "producer-trace")

    with pytest.raises(ValueError, match="producer trace is immutable"):
        ledger.attach_event(mutation.mutation_id, "rebuild-trace")
    assert ledger.list_mutations()[0]["event_trace_id"] == "producer-trace"


def test_legacy_rebuild_trace_repair_does_not_republish_reconciled_mutation(tmp_path):
    ledger = WikiProjectionLedger(tmp_path / "projection.db")
    page = tmp_path / "vault" / "page.md"
    page.parent.mkdir()
    page.write_text("# Reconciled scan mutation\n", encoding="utf-8")
    mutation = ledger.record_mutation(page, mutation_type="create")
    ledger.attach_event(mutation.mutation_id, "wiki-rebuild-old-synthetic-trace")
    for consumer in (
        "knowledge_graph",
        "cognitive_graph",
        "relation_embeddings",
        "wiki_search_index",
        "wiki_metrics",
        "moc_navigation",
    ):
        ledger.record_projection_receipt(
            mutation_id=mutation.mutation_id,
            consumer=consumer,
            outcome="ack",
            event_trace_id="wiki-rebuild-receipt-trace",
        )

    assert ledger.repair_synthetic_rebuild_event_traces() == 1
    assert ledger.list_mutations()[0]["event_trace_id"] == ""
    assert ledger.unpublished_mutations() == []


def test_historical_revision_cannot_rewind_current_pointer(tmp_path):
    ledger = WikiProjectionLedger(tmp_path / "projection.db")
    page = tmp_path / "vault" / "page.md"
    page.parent.mkdir()
    page.write_text("# Revision A\n", encoding="utf-8")
    first = ledger.record_mutation(page, mutation_type="create")
    page.write_text("# Revision B\n", encoding="utf-8")
    second = ledger.record_mutation(page, mutation_type="update")

    with ledger._conn() as conn:
        conn.execute(
            """UPDATE wiki_pages SET current_revision=?, content_sha256=?
               WHERE page_id=?""",
            (first.page_revision, first.content_sha256, first.page_id),
        )
        conn.commit()

    assert ledger.reconciliation_report()["pointer_gap"] == 1
    with pytest.raises(RuntimeError, match="refusing to move"):
        ledger.record_mutation(page, mutation_type="update")
    assert ledger.repair_current_pointers_from_history() == 1
    assert ledger.reconciliation_report()["pointer_gap"] == 0
    assert ledger.list_mutations()[-1]["page_revision"] == second.page_revision


def test_default_projection_ledger_is_isolated_from_user_home():
    ledger = WikiProjectionLedger()

    assert ledger.db_path != Path.home() / ".mnemos" / "wiki_projection.db"


def test_create_update_move_delete_keep_identity_and_projection_receipts(bus, tmp_path):
    from core.hephaestus.distillation_failure import publish_wiki_page_updated

    attempts = {"knowledge_graph": 0}

    def knowledge_graph(_event):
        attempts["knowledge_graph"] += 1
        if attempts["knowledge_graph"] == 1:
            return HandlerOutcome.retry("knowledge_graph", "injected restart-safe failure")
        return HandlerOutcome.ack("knowledge_graph")

    bus.subscribe("wiki_page_updated", knowledge_graph, consumer_id="knowledge_graph")
    bus.subscribe(
        "wiki_page_updated",
        lambda _event: HandlerOutcome.noop("cognitive_graph", "no self edge"),
        consumer_id="cognitive_graph",
    )
    bus.subscribe(
        "wiki_page_updated",
        lambda _event: HandlerOutcome.ack("relation_embeddings"),
        consumer_id="relation_embeddings",
    )
    bus.subscribe(
        "wiki_page_updated",
        lambda _event: HandlerOutcome.ack("wiki_search_index"),
        consumer_id="wiki_search_index",
    )
    bus.subscribe(
        "wiki_page_updated",
        lambda _event: HandlerOutcome.ack("wiki_metrics"),
        consumer_id="wiki_metrics",
    )
    bus.subscribe(
        "wiki_page_updated",
        lambda _event: HandlerOutcome.ack("moc_navigation"),
        consumer_id="moc_navigation",
    )

    vault = tmp_path / "vault"
    old_path = vault / "00-Inbox" / "page.md"
    old_path.parent.mkdir(parents=True)
    old_path.write_text("# Page\n\nfirst", encoding="utf-8")
    created = publish_wiki_page_updated(old_path, "create")
    _wait_for(lambda: _event_status(bus, created["event_trace_id"]) == "done")

    old_path.write_text("# Page\n\nsecond", encoding="utf-8")
    updated = publish_wiki_page_updated(old_path, "update")
    _wait_for(lambda: _event_status(bus, updated["event_trace_id"]) == "done")

    new_path = vault / "04-Concepts" / "page.md"
    new_path.parent.mkdir(parents=True)
    old_path.rename(new_path)
    moved = publish_wiki_page_updated(new_path, "move", previous_path=old_path)
    _wait_for(lambda: _event_status(bus, moved["event_trace_id"]) == "done")

    new_path.unlink()
    deleted = publish_wiki_page_updated(new_path, "delete")
    _wait_for(lambda: _event_status(bus, deleted["event_trace_id"]) == "done")

    assert {created["page_id"], updated["page_id"], moved["page_id"], deleted["page_id"]} == {
        created["page_id"]
    }
    assert len(
        {
            created["page_revision"],
            updated["page_revision"],
            moved["page_revision"],
            deleted["page_revision"],
        }
    ) == 4
    assert deleted["tombstone"] is True
    report = WikiProjectionLedger(tmp_path / "wiki_projection.db").reconciliation_report()
    assert report["projection_gap"] == 0
    assert report["mutation_count"] == 4
    retry_receipts = bus._get_conn().execute(
        """SELECT disposition FROM handler_receipts
           WHERE mutation_id=? AND consumer='knowledge_graph' ORDER BY id""",
        (created["mutation_id"],),
    ).fetchall()
    assert [row["disposition"] for row in retry_receipts] == ["retry", "ack"]


def test_full_scan_detects_unpublished_move_update_delete_without_identity_guessing(tmp_path):
    ledger = WikiProjectionLedger(tmp_path / "projection.db")
    vault = tmp_path / "vault"
    first = vault / "00-Inbox" / "first.md"
    second = vault / "00-Inbox" / "second.md"
    first.parent.mkdir(parents=True)
    first.write_text("# First\nunique", encoding="utf-8")
    second.write_text("# Second\nunique-2", encoding="utf-8")
    initial = ledger.reconcile_vault(vault)
    initial_ids = {Path(row["page_path"]).name: row["page_id"] for row in initial["mutations"]}
    assert initial["counts"] == {"create": 2, "update": 0, "move": 0, "delete": 0}

    moved = vault / "04-Concepts" / "first.md"
    moved.parent.mkdir(parents=True)
    first.rename(moved)
    second.unlink()
    moved.write_text("# First\nunique", encoding="utf-8")
    third = vault / "00-Inbox" / "third.md"
    third.write_text("# Third\nnew", encoding="utf-8")
    result = ledger.reconcile_vault(vault)
    assert result["counts"] == {"create": 1, "update": 0, "move": 1, "delete": 1}
    move = next(row for row in result["mutations"] if row["mutation_type"] == "move")
    assert move["page_id"] == initial_ids["first.md"]

    incremental = [
        {"page_id": row["page_id"], "revision": row["page_revision"]}
        for row in ledger.list_mutations()
    ]
    assert projection_snapshot_hash(incremental) == projection_snapshot_hash(reversed(incremental))


def test_revisiting_historical_content_advances_current_revision_pointer(tmp_path):
    ledger = WikiProjectionLedger(tmp_path / "projection.db")
    vault = tmp_path / "vault"
    page = vault / "04-Concepts" / "page.md"
    page.parent.mkdir(parents=True)

    page.write_text("# Revision A\n", encoding="utf-8")
    first = ledger.reconcile_vault(vault)
    page.write_text("# Revision B\n", encoding="utf-8")
    second = ledger.reconcile_vault(vault)
    page.write_text("# Revision A\n", encoding="utf-8")
    third = ledger.reconcile_vault(vault)
    page.write_text("# Revision B\n", encoding="utf-8")
    fourth = ledger.reconcile_vault(vault)
    fifth = ledger.reconcile_vault(vault)

    assert first["counts"]["create"] == 1
    assert second["counts"]["update"] == 1
    assert third["counts"]["update"] == 1
    assert fourth["counts"]["update"] == 1
    assert fifth["recorded_mutations"] == 0
    mutations = ledger.list_mutations()
    assert len(mutations) == 4
    assert len({row["page_revision"] for row in mutations}) == 4
    assert [row["parent_revision"] for row in mutations] == [
        "",
        mutations[0]["page_revision"],
        mutations[1]["page_revision"],
        mutations[2]["page_revision"],
    ]


def test_projection_receipts_cannot_ack_a_revision_before_its_predecessor(tmp_path):
    ledger = WikiProjectionLedger(tmp_path / "projection.db")
    page = tmp_path / "vault" / "page.md"
    page.parent.mkdir()
    page.write_text("# Revision A\n", encoding="utf-8")
    first = ledger.record_mutation(page, mutation_type="create")
    page.write_text("# Revision B\n", encoding="utf-8")
    second = ledger.record_mutation(page, mutation_type="update")

    with pytest.raises(RuntimeError, match="predecessor revision"):
        ledger.record_projection_receipt(
            mutation_id=second.mutation_id,
            consumer="knowledge_graph",
            outcome="ack",
        )

    ledger.record_projection_receipt(
        mutation_id=first.mutation_id,
        consumer="knowledge_graph",
        outcome="ack",
    )
    ledger.record_projection_receipt(
        mutation_id=second.mutation_id,
        consumer="knowledge_graph",
        outcome="ack",
    )
    assert ledger.reconciliation_report(("knowledge_graph",))["projection_gap"] == 0


def test_terminal_projection_receipt_cannot_regress_or_change_trace(tmp_path):
    ledger = WikiProjectionLedger(tmp_path / "projection.db")
    page = tmp_path / "page.md"
    page.write_text("# Page", encoding="utf-8")
    mutation = ledger.record_mutation(page, mutation_type="create")
    ledger.record_projection_receipt(
        mutation_id=mutation.mutation_id,
        consumer="knowledge_graph",
        outcome="ack",
        event_trace_id=mutation.mutation_id,
    )

    ledger.record_projection_receipt(
        mutation_id=mutation.mutation_id,
        consumer="knowledge_graph",
        outcome="retry",
        reason="late replay failure",
        event_trace_id=mutation.mutation_id,
    )
    report = ledger.reconciliation_report(("knowledge_graph",))
    assert report["ok"] is True
    with pytest.raises(ValueError, match="trace is immutable"):
        ledger.record_projection_receipt(
            mutation_id=mutation.mutation_id,
            consumer="knowledge_graph",
            outcome="ack",
            event_trace_id="forged-trace",
        )


def test_nonterminal_projection_receipt_cannot_erase_its_trace(tmp_path):
    ledger = WikiProjectionLedger(tmp_path / "projection.db")
    page = tmp_path / "page.md"
    page.write_text("# Page", encoding="utf-8")
    mutation = ledger.record_mutation(page, mutation_type="create")
    ledger.record_projection_receipt(
        mutation_id=mutation.mutation_id,
        consumer="wiki_search_index",
        outcome="retry",
        event_trace_id=mutation.mutation_id,
    )

    ledger.record_projection_receipt(
        mutation_id=mutation.mutation_id,
        consumer="wiki_search_index",
        outcome="retry",
        reason="retry remains pending",
        event_trace_id="",
    )

    receipt = ledger.projection_receipt(
        mutation.mutation_id, "wiki_search_index"
    )
    assert receipt is not None
    assert receipt["event_trace_id"] == mutation.mutation_id


def test_event_bus_recovers_pending_wiki_event_after_real_recreation(tmp_path, monkeypatch):
    monkeypatch.setattr("core.mnemos_bus._resolve_event_db_dir", lambda _config: tmp_path)
    monkeypatch.setattr(
        "core.mnemos_bus.resolve_wiki_projection_db_path",
        lambda _config=None: tmp_path / "wiki_projection.db",
    )
    page = tmp_path / "vault" / "page.md"
    page.parent.mkdir()
    page.write_text("# Restart-safe page\n", encoding="utf-8")
    mutation = WikiProjectionLedger(tmp_path / "wiki_projection.db").record_mutation(
        page, mutation_type="create"
    )
    first_bus = EventBus(root_dir=tmp_path / "events")
    first_bus.publish(
        Event(
            "wiki_page_updated",
            "test",
            {
                "page_path": str(page),
                "mutation_id": mutation.mutation_id,
                "page_revision": mutation.page_revision,
            },
            trace_id=mutation.mutation_id,
        )
    )
    first_bus.close()

    recovered = []
    second_bus = EventBus(root_dir=tmp_path / "events")
    second_bus.subscribe(
        "wiki_page_updated",
        lambda event: recovered.append(event.trace_id)
        or HandlerOutcome.ack("knowledge_graph"),
    )
    second_bus.start_dispatch()
    try:
        _wait_for(lambda: recovered == [mutation.mutation_id])
        _wait_for(lambda: _event_status(second_bus, mutation.mutation_id) == "done")
        assert WikiProjectionLedger(tmp_path / "wiki_projection.db").reconciliation_report(
            ("knowledge_graph",)
        )["projection_gap"] == 0
    finally:
        second_bus.stop_dispatch()
        second_bus.close()


def test_out_of_order_delivery_retries_until_predecessor_receipt_exists(bus, tmp_path):
    bus.stop_dispatch()
    ledger = WikiProjectionLedger(tmp_path / "wiki_projection.db")
    page = tmp_path / "vault" / "page.md"
    page.parent.mkdir()
    page.write_text("# Revision A\n", encoding="utf-8")
    first = ledger.record_mutation(page, mutation_type="create")
    page.write_text("# Revision B\n", encoding="utf-8")
    second = ledger.record_mutation(page, mutation_type="update")
    calls = []

    def consumer(event):
        calls.append(event.payload["mutation_id"])
        return HandlerOutcome.ack("knowledge_graph")

    bus.subscribe("wiki_page_updated", consumer, consumer_id="knowledge_graph")
    for mutation in (second, first):
        bus.publish(
            Event(
                "wiki_page_updated",
                "test",
                {
                    "page_path": mutation.page_path,
                    "mutation_id": mutation.mutation_id,
                    "page_revision": mutation.page_revision,
                },
                trace_id=mutation.mutation_id,
            )
        )
    bus.start_dispatch()

    _wait_for(lambda: _event_status(bus, first.mutation_id) == "done")
    _wait_for(lambda: _event_status(bus, second.mutation_id) == "done")
    assert calls == [first.mutation_id, second.mutation_id]
    second_row = bus._get_conn().execute(
        "SELECT retry_count FROM events WHERE trace_id=?", (second.mutation_id,)
    ).fetchone()
    assert second_row["retry_count"] == 0
    assert calls.count(first.mutation_id) == 1
    assert ledger.reconciliation_report(("knowledge_graph",))["projection_gap"] == 0


def test_partial_rebuild_receipts_resume_only_missing_consumers(bus, tmp_path):
    bus.stop_dispatch()
    ledger = WikiProjectionLedger(tmp_path / "wiki_projection.db")
    page = tmp_path / "vault" / "page.md"
    page.parent.mkdir()
    page.write_text("# Page", encoding="utf-8")
    mutation = ledger.record_mutation(page, mutation_type="create")
    ledger.record_projection_receipt(
        mutation_id=mutation.mutation_id,
        consumer="knowledge_graph",
        outcome="ack",
        event_trace_id="prior-rebuild",
    )
    calls = {"knowledge_graph": 0, "wiki_metrics": 0}

    def knowledge_graph(_event):
        calls["knowledge_graph"] += 1
        return HandlerOutcome.ack("knowledge_graph")

    def wiki_metrics(_event):
        calls["wiki_metrics"] += 1
        return HandlerOutcome.ack("wiki_metrics")

    bus.subscribe(
        "wiki_page_updated", knowledge_graph, consumer_id="knowledge_graph"
    )
    bus.subscribe("wiki_page_updated", wiki_metrics, consumer_id="wiki_metrics")
    bus.publish(
        Event(
            "wiki_page_updated",
            "recovery",
            {"mutation_id": mutation.mutation_id},
            trace_id=mutation.mutation_id,
        )
    )
    bus.start_dispatch()

    _wait_for(lambda: _event_status(bus, mutation.mutation_id) == "done")
    assert calls == {"knowledge_graph": 0, "wiki_metrics": 1}
    assert ledger.reconciliation_report(("knowledge_graph", "wiki_metrics"))["ok"]


def test_large_vault_reconciliation_keeps_every_page_and_is_idempotent(tmp_path):
    ledger = WikiProjectionLedger(tmp_path / "projection.db")
    vault = tmp_path / "vault"
    for index in range(1200):
        page = vault / f"group-{index // 100:02d}" / f"page-{index:04d}.md"
        page.parent.mkdir(parents=True, exist_ok=True)
        page.write_text(f"# Page {index}\n\nbody-{index}\n", encoding="utf-8")

    first = ledger.reconcile_vault(vault)
    second = ledger.reconcile_vault(vault)

    assert first["scanned_pages"] == 1200
    assert first["counts"] == {"create": 1200, "update": 0, "move": 0, "delete": 0}
    assert len({row["page_id"] for row in first["mutations"]}) == 1200
    assert second["recorded_mutations"] == 0


def test_vault_reconciliation_ignores_and_can_prune_hidden_projection_artifacts(tmp_path):
    ledger = WikiProjectionLedger(tmp_path / "projection.db")
    vault = tmp_path / "vault"
    visible = vault / "04-Concepts" / "visible.md"
    hidden = vault / ".kg" / "snapshots" / "generated.md"
    visible.parent.mkdir(parents=True)
    hidden.parent.mkdir(parents=True)
    visible.write_text("# Visible\n", encoding="utf-8")
    hidden.write_text("# Internal projection artifact\n", encoding="utf-8")

    # Simulate a ledger created by the pre-fix scanner, which treated .kg as Wiki content.
    ledger.record_mutation(hidden, mutation_type="create")
    result = ledger.reconcile_vault(vault)

    assert result["scanned_pages"] == 1
    assert result["counts"] == {"create": 1, "update": 0, "move": 0, "delete": 0}
    assert all(".kg" not in Path(row["page_path"]).parts for row in result["mutations"])

    pruned = ledger.prune_out_of_scope_pages(vault)
    assert pruned == {"pages": 1, "mutations": 1, "projection_receipts": 0}
    assert [Path(row["page_path"]).name for row in ledger.list_mutations()] == ["visible.md"]
