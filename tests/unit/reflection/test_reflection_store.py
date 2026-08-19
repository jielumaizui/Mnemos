import pytest
from datetime import datetime

from core.access_policy import AccessNarrowing, PrincipalEnvelope
from core.cognitive.access_control import make_cognitive_access_envelope
from core.reflection.models import (
    CognitiveShift,
    InsightSnapshot,
    MirrorSnapshot,
    ReflectionRecord,
    ReflectionTrigger,
)
from core.reflection.reflection_store import ReflectionStore


def _private_access(*, session_id: str = "reflection-session") -> dict:
    return make_cognitive_access_envelope(
        owner_principal_id="test:reflection-owner",
        owner_agent="codex",
        scope_type="session",
        scope_id=session_id,
        session_id=session_id,
        project="mnemos",
        purposes=("reflection_experience_read",),
        consent_provenance_refs=("sha256:" + "a" * 64,),
        sensitivity="sensitive",
        retention_policy="reflection_retention",
        source_acl_lineage=("sha256:" + "b" * 64,),
    )


def _principal() -> PrincipalEnvelope:
    return PrincipalEnvelope(
        principal_id="test:reflection-owner",
        agent="codex",
        host_kind="test",
        capability_id="reflection-test",
        capabilities=frozenset({"memory_read"}),
        allowed_projects=frozenset({"mnemos"}),
    )


def _make_record(record_id: str = "r1", trigger: ReflectionTrigger = ReflectionTrigger.NEW_PROJECT):
    return ReflectionRecord(
        id=record_id,
        created_at=datetime(2026, 6, 10, 10, 0, 0),
        trigger=trigger,
        trigger_event="触发事件",
        user_query="用户查询",
        mirror_dimensions=["attention", "decisions"],
        mirror_snapshots=[
            MirrorSnapshot(
                observation_id="obs-1",
                dimension="attention",
                value_summary="关注分布",
                evidence_summary="证据1",
                confidence=0.8,
                recency_weight=0.9,
                period_end=datetime(2026, 6, 9, 10, 0, 0),
            ),
            MirrorSnapshot(
                observation_id="obs-2",
                dimension="decisions",
                value_summary="决策模式",
                evidence_summary="证据2",
                confidence=0.7,
                recency_weight=0.8,
                period_end=datetime(2026, 6, 8, 10, 0, 0),
            ),
        ],
        insight=InsightSnapshot(
            summary="这是一个关键洞察",
            key_points=["要点1", "要点2"],
            dimensions_involved=["attention", "decisions"],
        ),
        temporal_context={"rhythm": "workday_morning"},
        fed_back_to_observations=True,
        fed_back_to_knowledge=False,
    )


def test_save_and_get_latest_round_trip(tmp_path):
    db_path = tmp_path / "reflections.db"
    store = ReflectionStore(str(db_path))

    record = _make_record("r1")
    assert store.save_record(record) is True

    latest = store.get_latest(limit=5)
    assert len(latest) == 1
    got = latest[0]
    assert got.id == "r1"
    assert got.trigger == ReflectionTrigger.NEW_PROJECT
    assert got.mirror_dimensions == ["attention", "decisions"]
    assert len(got.mirror_snapshots) == 2
    assert got.mirror_snapshots[0].dimension == "attention"
    assert got.insight.summary == "这是一个关键洞察"
    assert got.fed_back_to_observations is True
    assert got.fed_back_to_knowledge is False


def test_read_only_store_rejects_record_mutation(tmp_path):
    db_path = tmp_path / "reflections.db"
    writable = ReflectionStore(str(db_path))
    read_only = ReflectionStore(
        str(db_path),
        initialize=False,
        read_only=True,
    )

    with pytest.raises(PermissionError, match="read-only ReflectionStore"):
        read_only.save_record(_make_record("blocked"))

    assert writable.get_latest(limit=5) == []


def test_get_by_trigger_filters_by_trigger_type(tmp_path):
    db_path = tmp_path / "reflections.db"
    store = ReflectionStore(str(db_path))

    store.save_record(_make_record("r-new", ReflectionTrigger.NEW_PROJECT))
    store.save_record(_make_record("r-major", ReflectionTrigger.MAJOR_DECISION))
    store.save_record(_make_record("r-manual", ReflectionTrigger.MANUAL))

    new_project_records = store.get_by_trigger(ReflectionTrigger.NEW_PROJECT, limit=10)
    assert len(new_project_records) == 1
    assert new_project_records[0].id == "r-new"

    major_records = store.get_by_trigger(ReflectionTrigger.MAJOR_DECISION, limit=10)
    assert len(major_records) == 1
    assert major_records[0].id == "r-major"


def test_get_shifts_round_trip(tmp_path):
    db_path = tmp_path / "reflections.db"
    store = ReflectionStore(str(db_path))

    record = _make_record("r-shift")
    store.save_record(record)

    shift = CognitiveShift(
        dimension="growth",
        shift_type="role_change",
        from_state="开发者",
        to_state="技术负责人",
        confidence=0.85,
        evidence=["承担技术决策", "带领小团队"],
        first_seen_at=datetime(2026, 1, 1, 0, 0, 0),
        shift_detected_at=datetime(2026, 6, 10, 0, 0, 0),
    )
    store.save_shift(shift, reflection_id="r-shift")

    all_shifts = store.get_shifts(limit=10)
    assert len(all_shifts) == 1
    got = all_shifts[0]
    assert got.dimension == "growth"
    assert got.from_state == "开发者"
    assert got.to_state == "技术负责人"
    assert got.confidence == pytest.approx(0.85)
    assert got.related_reflection_id == "r-shift"

    dim_shifts = store.get_shifts(dimension="growth", limit=10)
    assert len(dim_shifts) == 1

    empty_shifts = store.get_shifts(dimension="stress", limit=10)
    assert len(empty_shifts) == 0


def test_projection_queries_use_complete_store_denominators(tmp_path):
    store = ReflectionStore(str(tmp_path / "reflections.db"))
    for index in range(3):
        record = _make_record(f"projection-{index}")
        record.created_at = datetime(2026, 6, 10 + index, 10, 0, 0)
        store.save_record(record)
        store.save_shift(
            CognitiveShift(
                dimension=f"dimension-{index}",
                shift_type="change",
                from_state="before",
                to_state="after",
                confidence=0.8,
                evidence=[record.id],
                first_seen_at=record.created_at,
                shift_detected_at=record.created_at,
            ),
            reflection_id=record.id,
        )

    assert len(store.get_latest(limit=1)) == 1
    assert len(store.get_shifts(limit=1)) == 1
    assert len(store.get_all_for_projection()) == 3
    assert len(store.get_all_shifts_for_projection()) == 3


def test_get_latest_respects_limit_and_order(tmp_path):
    db_path = tmp_path / "reflections.db"
    store = ReflectionStore(str(db_path))

    for i in range(5):
        record = _make_record(f"r-{i}")
        record.created_at = datetime(2026, 6, 10, 10, 0, 0)
        record.id = f"r-{i}"
        store.save_record(record)

    latest = store.get_latest(limit=3)
    assert len(latest) == 3


def test_add_experience(tmp_path):
    """add_experience 应写入 layer5_experiences 表并返回正整数 id。"""
    db_path = tmp_path / "reflections.db"
    store = ReflectionStore(str(db_path))

    experience = {
        "type": "cognitive_shift",
        "dimension": "growth",
        "dimensions": ["growth"],
        "trigger": "major_decision",
        "confidence": 0.85,
        "from_state": "开发者",
        "to_state": "技术负责人",
        "evidence": ["e1", "e2"],
    }
    eid = store.add_experience(experience)
    assert isinstance(eid, int)
    assert eid > 0

    import sqlite3

    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row  # noqa
    row = conn.execute(
        "SELECT type, dimension, trigger, confidence, from_state, to_state FROM layer5_experiences WHERE id = ?",  # noqa: E501
        (eid,),
    ).fetchone()
    assert row is not None
    assert row["type"] == "cognitive_shift"
    assert row["dimension"] == "growth"
    assert row["trigger"] == "major_decision"
    assert row["confidence"] == pytest.approx(0.85)
    assert row["from_state"] == "开发者"
    assert row["to_state"] == "技术负责人"


def test_get_experiences_round_trip(tmp_path):
    """get_experiences 应按时间倒序返回 Layer 5 经验"""
    db_path = tmp_path / "reflections.db"
    store = ReflectionStore(str(db_path))

    for i in range(3):
        store.add_experience(
            {
                "type": "insight_pattern",
                "dimension": "attention",
                "summary": f"洞察摘要{i}",
                "confidence": 0.75 + i * 0.05,
            }
        )

    experiences = store.get_experiences(limit=10)
    assert len(experiences) == 3
    # 时间倒序
    assert experiences[0]["summary"] == "洞察摘要2"
    assert experiences[0]["type"] == "insight_pattern"
    assert experiences[0]["dimension"] == "attention"
    assert experiences[0]["confidence"] == pytest.approx(0.85)

    filtered = store.get_experiences(type="insight_pattern", limit=10)
    assert len(filtered) == 3

    empty = store.get_experiences(type="not_exist", limit=10)
    assert empty == []


def test_layer5_experience_has_acl_authorized_read_and_scoped_delete(tmp_path):
    store = ReflectionStore(str(tmp_path / "reflections.db"))
    experience_id = store.add_experience(
        {
            "type": "insight_pattern",
            "dimension": "attention",
            "summary": "private Layer-5 cognition",
            "access_control": _private_access(),
        }
    )

    allowed, allowed_summary = store.authorized_get_experiences(
        principal=_principal(),
        narrowing=AccessNarrowing(
            session_id="reflection-session",
            project="mnemos",
        ),
        purpose="reflection_experience_read",
    )
    denied, denied_summary = store.authorized_get_experiences(
        principal=_principal(),
        narrowing=AccessNarrowing(
            session_id="other-session",
            project="mnemos",
        ),
        purpose="reflection_experience_read",
    )

    assert [item["id"] for item in allowed] == [experience_id]
    assert allowed_summary["authorized_count"] == 1
    assert denied == []
    assert denied_summary["denied_by_reason"] == {"session_scope_mismatch": 1}

    deleted = store.delete_subject_scope(
        request_id="delete-layer5-private",
        scope_kind="session",
        scope_value="reflection-session",
    )
    assert deleted["verified"] is True
    assert deleted["target_count"] == 1
    assert store.get_experiences() == []


def test_feedback_history_is_quarantined_from_active_experience_and_shift_reads(
    tmp_path,
):
    store = ReflectionStore(str(tmp_path / "reflections.db"))
    active_experience_id = store.add_experience(
        {
            "type": "insight_pattern",
            "dimension": "attention",
            "summary": "active non-feedback experience",
            "source_event_id": "raw-cognitive-source:layer5",
            "access_control": _private_access(),
        }
    )
    store.add_experience(
        {
            "type": "outcome_feedback",
            "dimension": "attention",
            "summary": "legacy feedback experience",
            "source_event_id": "feedback-history:layer5",
            "access_control": _private_access(),
        }
    )
    active_shift = CognitiveShift(
        dimension="growth",
        shift_type="role_change",
        from_state="a",
        to_state="b",
        confidence=0.8,
        evidence=["active"],
        first_seen_at=datetime(2026, 7, 17, 0, 0, 0),
        shift_detected_at=datetime(2026, 7, 18, 0, 0, 0),
        access_control=_private_access(),
    )
    legacy_shift = CognitiveShift(
        dimension="growth",
        shift_type="outcome_feedback",
        from_state="b",
        to_state="c",
        confidence=0.8,
        evidence=["legacy"],
        first_seen_at=datetime(2026, 7, 17, 1, 0, 0),
        shift_detected_at=datetime(2026, 7, 18, 1, 0, 0),
        access_control=_private_access(),
    )
    store.save_shift(active_shift, source_event_id="raw-cognitive-source:shift")
    store.save_shift(legacy_shift, source_event_id="feedback-history:shift")

    assert [item["id"] for item in store.get_experiences()] == [
        active_experience_id
    ]
    assert [item.to_state for item in store.get_shifts()] == ["b"]
    authorized_experiences, _ = store.authorized_get_experiences(
        principal=_principal(),
        narrowing=AccessNarrowing(
            session_id="reflection-session",
            project="mnemos",
        ),
        purpose="reflection_experience_read",
    )
    authorized_shifts, _ = store.authorized_get_shifts(
        principal=_principal(),
        narrowing=AccessNarrowing(
            session_id="reflection-session",
            project="mnemos",
        ),
        purpose="reflection_experience_read",
    )
    assert [item["id"] for item in authorized_experiences] == [
        active_experience_id
    ]
    assert [item.to_state for item in authorized_shifts] == ["b"]
