from __future__ import annotations

from pathlib import Path

import pytest

from core.cognitive.access_control import make_cognitive_access_envelope


class _Config:
    def __init__(self, root: Path):
        self.mnemos_dir = root / "mnemos"
        self.database_dir = self.mnemos_dir

    def get(self, key, default=None):
        values = {
            "event_bus.max_retries": 2,
            "event_bus.queue_depth_alert": 1000,
            "event_bus.max_queue_depth": 1000,
            "event_bus.max_recover_events": 1000,
            "event_bus.dead_letter_alert": 100,
            "event_bus.dead_letter_max": 1000,
            "event_bus.max_latency_ms": 10,
            "event_bus.dispatch_workers": 1,
            "event_bus.handler_timeout_seconds": 0,
            "event_bus.retry_base_seconds": 0,
            "event_bus.retry_max_seconds": 0,
        }
        return values.get(key, default)


def _provenance(session_id: str) -> dict:
    return make_cognitive_access_envelope(
        owner_principal_id="mcp:codex:event-test",
        owner_agent="codex",
        scope_type="session",
        scope_id=session_id,
        session_id=session_id,
        project="mnemos",
        purposes=("event_consume",),
        consent_provenance_refs=("raw:event-test",),
        sensitivity="sensitive",
        retention_policy="test_retention",
        source_acl_lineage=("sha256:" + "c" * 64,),
        visibility="private",
    )


def test_subject_delete_removes_tracked_event_and_blocks_queued_replay(tmp_path, monkeypatch):
    from core.mnemos_bus import Event, EventBus
    from core.ops.event_subject_provenance import delete_event_subject_scope

    config = _Config(tmp_path)
    monkeypatch.setattr("core.mnemos_bus.get_config", lambda: config)
    bus = EventBus()
    dispatched: list[str] = []
    bus.subscribe("private.event", lambda event: dispatched.append(event.payload["body"]))
    event = Event(
        event_type="private.event",
        source="test",
        payload={"body": "private event body"},
        trace_id="event-provenance-1",
        subject_provenance=_provenance("event-session"),
    )
    try:
        bus.publish(event, force=True)
        result = delete_event_subject_scope(
            db_path=bus._db_path,
            request_id="delete-event-1",
            scope_kind="session",
            scope_value="event-session",
        )

        assert result["status"] == "applied"
        assert result["verified"] is True
        assert result["events_deleted"] == 1
        assert bus.replay_dead_letter(event.trace_id) is False
        bus._dispatch_event(event)
        assert dispatched == []
        with pytest.raises(PermissionError, match="tombstoned"):
            bus.publish(event, force=True)
    finally:
        bus.close()


def test_event_subject_delete_never_guesses_unattributed_payload(tmp_path, monkeypatch):
    from core.mnemos_bus import Event, EventBus
    from core.ops.event_subject_provenance import delete_event_subject_scope

    config = _Config(tmp_path)
    monkeypatch.setattr("core.mnemos_bus.get_config", lambda: config)
    bus = EventBus()
    event = Event(
        event_type="legacy.event",
        source="test",
        payload={"body": "could belong to any session"},
        trace_id="event-unattributed-1",
    )
    try:
        bus.publish(event, force=True)
        result = delete_event_subject_scope(
            db_path=bus._db_path,
            request_id="delete-event-legacy",
            scope_kind="session",
            scope_value="event-session",
        )

        assert result["status"] == "applied"
        assert result["events_deleted"] == 0
        assert result["unresolved_legacy_count"] == 1
        assert result["verified"] is False
    finally:
        bus.close()


def test_recovered_event_dead_letter_preserves_existing_provenance(tmp_path, monkeypatch):
    """A row reloaded without an ACL must keep its immutable sidecar on DLQ move."""

    from core.mnemos_bus import Event, EventBus
    from core.ops.event_subject_provenance import delete_event_subject_scope

    config = _Config(tmp_path)
    monkeypatch.setattr("core.mnemos_bus.get_config", lambda: config)
    bus = EventBus()
    event = Event(
        event_type="recoverable.private.event",
        source="test",
        payload={"body": "private event body"},
        trace_id="event-provenance-recovered",
        subject_provenance=_provenance("recovered-event-session"),
    )
    try:
        # A consumer at publish time makes this a normal durable event.  The
        # recovered row intentionally does not expose its sidecar ACL.
        bus.subscribe(event.event_type, lambda _event: None)
        bus.publish(event, force=True)
        row = bus._get_conn().execute(
            "SELECT * FROM events WHERE trace_id=?", (event.trace_id,)
        ).fetchone()
        recovered = Event.from_row(row)
        assert recovered is not None
        with bus._handlers_lock:
            bus._handlers.clear()

        bus._dispatch_event(recovered)

        deletion = delete_event_subject_scope(
            db_path=bus._db_path,
            request_id="delete-recovered-event",
            scope_kind="session",
            scope_value="recovered-event-session",
        )
        assert deletion["status"] == "applied"
        assert deletion["verified"] is True
        assert deletion["dead_letters_deleted"] == 1
    finally:
        bus.close()
