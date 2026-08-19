from __future__ import annotations

from datetime import datetime
from unittest.mock import MagicMock

import pytest

from core.access_policy import AccessNarrowing, PrincipalEnvelope
from core.cognitive.access_control import make_cognitive_access_envelope
from core.cognitive.models import Dimension, Observation, ObservationType, SourceType
from core.reflection.mirror_engine import MirrorEngine
from core.reflection.models import CognitiveShift, ReflectionRecord, ReflectionTrigger
from core.reflection.reflection_store import ReflectionStore
from core.reflection.time_awareness import TemporalContext, TimeAwareness


def _principal(agent: str = "codex") -> PrincipalEnvelope:
    return PrincipalEnvelope(
        principal_id=f"mcp:{agent}:reflection",
        agent=agent,
        host_kind="test",
        capability_id="reflection-test",
        capabilities=frozenset({"memory_read", "memory_write"}),
        allowed_projects=frozenset({"mnemos"}),
    )


def _access(
    record_id: str,
    *,
    owner_agent: str = "codex",
    session_id: str = "session-1",
) -> dict:
    return make_cognitive_access_envelope(
        owner_principal_id="mcp:codex:reflection",
        owner_agent=owner_agent,
        scope_type="reflection",
        scope_id=record_id,
        session_id=session_id,
        project="mnemos",
        purposes=(
            "reflection_read",
            "reflection_feedback",
            "reflection_prompt",
            "reflection_experience_read",
            "reflection_export",
        ),
        consent_provenance_refs=("raw:reflection-acl-test",),
        sensitivity="sensitive",
        retention_policy="reflection_retention",
        source_acl_lineage=("sha256:" + "a" * 64,),
        visibility="private",
    )


def _record(record_id: str, access_control: dict | None = None) -> ReflectionRecord:
    return ReflectionRecord(
        id=record_id,
        created_at=datetime(2026, 7, 16, 9, 0, 0),
        trigger=ReflectionTrigger.MANUAL,
        user_query=f"private query {record_id}",
        access_control=access_control or {},
    )


def test_authorized_reflection_read_filters_headers_before_record_hydration(tmp_path) -> None:
    store = ReflectionStore(str(tmp_path / "reflections.db"))
    store.save_record(_record("allowed", _access("allowed")))
    store.save_record(_record("denied", _access("denied", owner_agent="claude")))

    original = store._row_to_record
    store._row_to_record = MagicMock(wraps=original)  # type: ignore[method-assign]
    records, summary = store.authorized_get_latest(
        principal=_principal(),
        narrowing=AccessNarrowing(session_id="session-1", project="mnemos"),
        purpose="reflection_read",
        limit=10,
    )

    assert [record.id for record in records] == ["allowed"]
    assert summary["denied_by_reason"] == {"owner_agent_mismatch": 1}
    assert store._row_to_record.call_count == 1  # type: ignore[union-attr]


def test_legacy_reflection_without_an_acl_is_not_returned(tmp_path) -> None:
    store = ReflectionStore(str(tmp_path / "reflections.db"))
    store.save_record(_record("legacy"))

    records, summary = store.authorized_get_latest(
        principal=_principal(),
        narrowing=AccessNarrowing(session_id="session-1", project="mnemos"),
        purpose="reflection_read",
    )

    assert records == []
    assert summary["denied_by_reason"] == {"acl_scope_unresolved": 1}


def test_mirror_requires_a_principal_before_authorized_observation_read() -> None:
    observation = Observation(
        id="obs-1",
        dimension=Dimension.DECISIONS,
        observation_type=ObservationType.PATTERN,
        value={"choice": "private"},
        confidence=0.8,
        source_type=SourceType.RAW,
        evidence=["private evidence"],
        period_end=datetime(2026, 7, 15, 9, 0, 0),
    )
    store = MagicMock()
    store.authorized_query.return_value = ([observation], {"authorized_count": 1})
    time_awareness = MagicMock(spec=TimeAwareness)
    time_awareness.get_temporal_context.return_value = TemporalContext(
        now=datetime(2026, 7, 16, 9, 0, 0),
        now_str="2026-07-16 09:00",
        rhythm="normal",
        rhythm_description="normal",
    )
    time_awareness.recency_weight.return_value = 0.9

    engine = MirrorEngine(observation_store=store, time_awareness=time_awareness)
    denied = engine.build_mirror("major_decision")

    assert denied.snapshots == []
    store.authorized_query.assert_not_called()

    allowed = engine.build_mirror(
        "major_decision",
        principal=_principal(),
        narrowing=AccessNarrowing(session_id="session-1", project="mnemos"),
    )

    assert allowed.snapshots
    assert store.authorized_query.call_args.kwargs["purpose"] == "reflection_prompt"


def test_scoped_reflection_delete_uses_acl_headers_and_keeps_legacy_residual_explicit(
    tmp_path,
) -> None:
    store = ReflectionStore(str(tmp_path / "reflections.db"))
    deleted_record = _record("delete-me", _access("delete-me"))
    retained_record = _record("keep-me", _access("keep-me", session_id="session-2"))
    store.save_record(deleted_record)
    store.save_record(retained_record)
    store.save_shift(
        CognitiveShift(
            dimension="decisions",
            shift_type="test",
            from_state="before",
            to_state="after",
            confidence=1.0,
            evidence=["test"],
            first_seen_at=None,
        ),
        reflection_id="delete-me",
    )
    # Layer-5 rows have no object ACL; scoped deletion must not guess that
    # this row belongs to the selected session, and must not claim verified.
    store.add_experience({"type": "legacy", "summary": "unscoped legacy"})

    result = store.delete_subject_scope(
        request_id="reflection-delete-test",
        scope_kind="session",
        scope_value="session-1",
    )

    assert result["status"] == "applied"
    assert result["reflection_records_deleted"] == 1
    assert result["cognitive_shifts_deleted"] == 1
    assert result["legacy_unscoped_layer5_count"] == 1
    assert result["verified"] is False
    assert store.get_by_id("delete-me") is None
    assert store.get_by_id("keep-me") is not None
    assert store.get_shifts(limit=10) == []
    with pytest.raises(PermissionError, match="subject-deleted"):
        store.save_record(deleted_record)

    retry = store.delete_subject_scope(
        request_id="reflection-delete-test-retry",
        scope_kind="session",
        scope_value="session-1",
    )
    assert retry["status"] == "existing"
    assert retry["verified"] is False
