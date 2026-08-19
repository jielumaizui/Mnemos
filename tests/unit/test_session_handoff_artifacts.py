import hashlib
from types import SimpleNamespace

import pytest

from core.evidence.artifact_capture import write_managed_capture_artifact
from core.evidence.artifact_uri import build_artifact_uri
from core.sync_framework.agent_source import SessionInfo, Turn
from core.sync_framework.session_handoff import (
    build_session_artifact_refs,
    build_session_messages,
    build_session_raw_event_refs,
    enqueue_complete_session,
)


def test_sync_session_handoff_carries_revision_spans_and_artifact_catalog_sources(tmp_path):
    attachment = tmp_path / "design.md"
    attachment.write_text("approved design", encoding="utf-8")
    turn = Turn(
        turn_number=4,
        user_content="review this",
        assistant_content="approved",
        metadata={
            "raw_event_id": "raw-revision-4",
            "raw_content_hash": "content-hash-4",
        },
        tool_results=[{"tool_name": "review", "result": "approved"}],
        attachments=[{"path": str(attachment), "mime_type": "text/markdown"}],
    )

    messages = build_session_messages([turn])
    raw_refs = build_session_raw_event_refs([turn])
    artifact_refs = build_session_artifact_refs(
        source_agent="codex",
        session_id="session-1",
        turns=[turn],
    )

    assert messages[0]["source_span"] == {
        "revision_id": "raw-revision-4",
        "turn_number": 4,
        "content_hash": "content-hash-4",
        "role": "user",
        "span_start": 0,
        "span_end": len("review this"),
    }
    assert messages[1]["source_span"]["span_start"] == len("review this")
    assert messages[1]["source_span"]["span_end"] == len("review thisapproved")
    assert raw_refs == [
        {
            "revision_id": "raw-revision-4",
            "turn_number": 4,
            "content_hash": "content-hash-4",
            "span_start": 0,
            "span_end": len("review thisapproved"),
        }
    ]
    assert {ref["artifact_type"] for ref in artifact_refs} == {
        "tool_result",
        "attachment",
    }
    assert all(ref["source_event_id"] == "raw-revision-4" for ref in artifact_refs)
    assert all(len(ref["sha256"]) == 64 for ref in artifact_refs)


def test_sync_session_handoff_fails_closed_without_authoritative_revision(tmp_path):
    attachment = tmp_path / "design.md"
    attachment.write_text("approved design", encoding="utf-8")
    turn = Turn(
        turn_number=1,
        user_content="review",
        assistant_content="done",
        attachments=[{"path": str(attachment)}],
    )

    with pytest.raises(ValueError, match="authoritative Raw revision"):
        build_session_messages([turn])
    with pytest.raises(ValueError, match="authoritative Raw revision"):
        build_session_raw_event_refs([turn])
    with pytest.raises(ValueError, match="authoritative Raw revision"):
        build_session_artifact_refs(
            source_agent="codex",
            session_id="session-1",
            turns=[turn],
        )


def test_sync_session_handoff_preserves_parser_supplied_artifact_refs(tmp_path):
    screenshot = tmp_path / "screen.png"
    screenshot.write_bytes(b"screenshot bytes")
    digest = hashlib.sha256(screenshot.read_bytes()).hexdigest()
    supplied = {
        "uri": build_artifact_uri("codex", "session-1", 2, "screenshot", 0),
        "artifact_type": "screenshot",
        "summary": "parser screenshot",
        "source_event_id": "provisional",
        "path": str(screenshot),
        "sha256": digest,
        "mime_type": "image/png",
    }
    turn = Turn(
        turn_number=2,
        user_content="inspect",
        assistant_content="done",
        metadata={
            "raw_event_id": "raw-revision-2",
            "raw_content_hash": "content-hash-2",
            "artifact_refs": [supplied],
        },
    )

    refs = build_session_artifact_refs(
        source_agent="codex",
        session_id="session-1",
        turns=[turn],
    )

    assert len(refs) == 1
    assert refs[0]["artifact_type"] == "screenshot"
    assert refs[0]["source_event_id"] == "raw-revision-2"
    assert refs[0]["source_event_ids"] == ["raw-revision-2"]


def test_sync_session_handoff_preserves_malformed_refs_for_catalog_rejection():
    from core.evidence.artifact_catalog import ArtifactCatalog

    turn = Turn(
        turn_number=3,
        user_content="inspect",
        assistant_content="done",
        metadata={
            "raw_event_id": "raw-revision-3",
            "raw_content_hash": "content-hash-3",
            "artifact_refs": {"unexpected": "mapping"},
        },
    )

    refs = build_session_artifact_refs(
        source_agent="codex",
        session_id="session-1",
        turns=[turn],
    )
    catalog = ArtifactCatalog.from_refs(
        refs,
        allowed_source_event_ids=("raw-revision-3",),
    )

    assert catalog.rejection_codes == ("artifact_type_invalid",)


def test_complete_session_handoff_passes_managed_artifact_owner_root(
    tmp_path,
    monkeypatch,
):
    artifact = write_managed_capture_artifact(
        database_dir=tmp_path,
        source_agent="codex",
        session_id="session-1",
        turn_number=0,
        artifact_type="capture",
        content="system-owned capture bytes",
    )
    turn = Turn(
        turn_number=0,
        user_content="review",
        assistant_content="done",
        metadata={
            "raw_event_id": "raw-revision-0",
            "raw_content_hash": "content-hash-0",
            "artifact_path": str(artifact),
        },
    )
    captured: dict = {}

    class _Receipt:
        input_revision = "revision"
        task_id = "task"

        @staticmethod
        def to_dict():
            return {"task_id": "task", "input_revision": "revision"}

    def enqueue_with_receipt(**kwargs):
        captured.update(kwargs)
        return _Receipt()

    monkeypatch.setattr(
        "core.kia.amphora.enqueue_with_receipt",
        enqueue_with_receipt,
    )
    monkeypatch.setattr(
        "core.ops.cognitive_pipeline_receipts.record_sync_handoff",
        lambda *_args, **_kwargs: None,
    )

    result = enqueue_complete_session(
        database_dir=tmp_path,
        source=SimpleNamespace(name="codex"),
        session_info=SessionInfo(
            session_id="session-1",
            source_path=tmp_path / "native.jsonl",
        ),
        turns=[turn],
    )

    assert result["task_id"] == "task"
    refs = captured["meta"]["artifact_refs"]
    assert len(refs) == 1
    assert refs[0]["path"] == str(artifact)
    assert refs[0]["sha256"] == hashlib.sha256(artifact.read_bytes()).hexdigest()
