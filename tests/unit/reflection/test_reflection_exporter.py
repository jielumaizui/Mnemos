from datetime import datetime

from core.reflection.models import (
    CognitiveShift,
    InsightSnapshot,
    MirrorSnapshot,
    ReflectionRecord,
    ReflectionTrigger,
    UserFeedback,
    FeedbackType,
)
from core.reflection.feedback_loop import knowledge_update_from_shift
from core.reflection.reflection_exporter import ReflectionExporter
from core.reflection.reflection_store import ReflectionStore


def test_export_record_creates_markdown(tmp_path):
    exporter = ReflectionExporter(str(tmp_path))

    record = ReflectionRecord(
        id="abc123",
        created_at=datetime(2026, 6, 10, 10, 30),
        trigger=ReflectionTrigger.MAJOR_DECISION,
        trigger_event="用户启动项目 X",
        user_query="我要启动新项目 X",
        mirror_dimensions=["decisions", "growth"],
        mirror_snapshots=[
            MirrorSnapshot(
                observation_id="obs-1",
                dimension="decisions",
                value_summary="决策信号 10 次",
                evidence_summary="典型情境: 项目启动",
                confidence=0.85,
                recency_weight=0.9,
                period_end=datetime(2026, 6, 9),
            )
        ],
        insight=InsightSnapshot(
            summary="这是一个关键决策时刻",
            key_points=["进入新领域", "资源投入较大"],
            dimensions_involved=["decisions", "growth"],
        ),
        temporal_context={"rhythm": "morning"},
        internal_validation={"overall_score": 0.92, "passed": True, "findings": []},
        fed_back_to_observations=True,
        fed_back_to_knowledge=False,
    )

    path = exporter.export_record(record)

    assert path.exists()
    assert path.parent == tmp_path / "L4-Reflections" / "Reflections" / "2026-06-10"
    content = path.read_text(encoding="utf-8")
    assert "Reflection abc123" in content
    assert "decisions" in content
    assert "关键决策时刻" in content
    assert "L4-Reflections" not in content  # directory only
    assert "source_count: 2" in content
    assert "reflection-store:reflections/abc123" in content
    assert "observation:obs-1" in content


def test_export_shifts_group_by_dimension(tmp_path):
    exporter = ReflectionExporter(str(tmp_path))

    shifts = [
        CognitiveShift(
            dimension="growth",
            shift_type="role_change",
            from_state="开发者",
            to_state="技术负责人",
            confidence=0.8,
            evidence=["承担技术决策"],
            first_seen_at=datetime(2026, 1, 1),
            shift_detected_at=datetime(2026, 6, 1),
        ),
        CognitiveShift(
            dimension="growth",
            shift_type="style_evolution",
            from_state="独立贡献",
            to_state="团队协调",
            confidence=0.7,
            evidence=["频繁组织会议"],
            first_seen_at=datetime(2026, 3, 1),
            shift_detected_at=datetime(2026, 6, 2),
        ),
        CognitiveShift(
            dimension="attention",
            shift_type="focus_shift",
            from_state="前端",
            to_state="后端",
            confidence=0.6,
            evidence=["讨论后端架构"],
            first_seen_at=datetime(2026, 5, 1),
            shift_detected_at=datetime(2026, 6, 3),
        ),
    ]

    written = exporter.export_shifts(shifts)

    assert set(written.keys()) == {"growth", "attention"}
    growth_path = written["growth"]
    assert growth_path == tmp_path / "L4-Reflections" / "Shifts" / "growth.md"
    content = growth_path.read_text(encoding="utf-8")
    assert "开发者" in content
    assert "技术负责人" in content
    assert "独立贡献" in content
    assert len(content.split("###")) >= 3
    assert "source_count: 2" in content
    assert "reflection-store:cognitive_shifts/growth/" in content


def test_export_weekly_report(tmp_path):
    exporter = ReflectionExporter(str(tmp_path))

    records = [
        ReflectionRecord(
            id="r1",
            created_at=datetime(2026, 6, 8, 10, 0),  # Monday
            trigger=ReflectionTrigger.NEW_PROJECT,
            trigger_event="新项目",
            user_query="启动",
            mirror_dimensions=["attention"],
            insight=InsightSnapshot(summary="启动新项目", key_points=[], dimensions_involved=[]),
        ),
        ReflectionRecord(
            id="r2",
            created_at=datetime(2026, 6, 9, 10, 0),
            trigger=ReflectionTrigger.MAJOR_DECISION,
            trigger_event="决策",
            user_query="决定",
            mirror_dimensions=["decisions"],
            insight=InsightSnapshot(summary="重大决策", key_points=[], dimensions_involved=[]),
        ),
    ]

    shifts = [
        CognitiveShift(
            dimension="attention",
            shift_type="focus_shift",
            from_state="A",
            to_state="B",
            confidence=0.7,
            evidence=["evidence"],
            first_seen_at=datetime(2026, 5, 1),
            shift_detected_at=datetime(2026, 6, 8),
        )
    ]

    path = exporter.export_weekly_report(records, shifts=shifts)

    assert path.exists()
    assert path.name == "weekly-2026-06-08.md"
    content = path.read_text(encoding="utf-8")
    assert "new_project" in content
    assert "major_decision" in content
    assert "attention" in content
    assert "启动新项目" in content
    assert "重大决策" in content
    assert "source_count: 2" in content
    assert "reflection-store:reflections/r1" in content
    assert "reflection-store:reflections/r2" in content


def test_export_record_with_user_feedback(tmp_path):
    exporter = ReflectionExporter(str(tmp_path))

    record = ReflectionRecord(
        id="r3",
        created_at=datetime(2026, 6, 10),
        trigger=ReflectionTrigger.MANUAL,
        trigger_event="用户手动触发反思",
        user_query="分析当前决策模式",
        insight=InsightSnapshot(
            summary="决策模式分析结果",
            key_points=["结论一"],
            dimensions_involved=["decisions"],
        ),
        user_feedback=UserFeedback(
            feedback_type=FeedbackType.ACCURATE,
            comment="非常准确",
            given_at=datetime(2026, 6, 11),
        ),
    )

    path = exporter.export_record(record)
    assert path is not None
    content = path.read_text(encoding="utf-8")
    assert "accurate" in content
    assert "非常准确" in content


def test_export_all_from_store(tmp_path):
    from core.reflection.reflection_store import ReflectionStore

    db_path = tmp_path / "reflections.db"
    store = ReflectionStore(str(db_path))
    record = ReflectionRecord(
        id="all1",
        created_at=datetime(2026, 6, 10),
        trigger=ReflectionTrigger.SCHEDULED,
        trigger_event="定时反思触发",
        user_query="分析压力变化",
        mirror_dimensions=["stress"],
        insight=InsightSnapshot(
            summary="压力水平上升分析", key_points=["deadline 影响"], dimensions_involved=["stress"]
        ),
    )
    store.save_record(record)
    shift = CognitiveShift(
        dimension="stress",
        shift_type="level_change",
        from_state="low",
        to_state="high",
        confidence=0.8,
        evidence=["deadline"],
        first_seen_at=datetime(2026, 5, 1),
        shift_detected_at=datetime(2026, 6, 10),
    )
    store.save_shift(shift, record.id)

    exporter = ReflectionExporter(str(tmp_path))
    stats = exporter.export_all(store)

    assert stats["records"] == 1
    assert stats["shifts"] == 1
    assert (tmp_path / "L4-Reflections" / "Reflections" / "2026-06-10" / "all1.md").exists()
    assert (tmp_path / "L4-Reflections" / "Shifts" / "stress.md").exists()
    assert list((tmp_path / "L4-Reflections" / "Reports").glob("weekly-*.md"))


def test_export_all_preserves_unowned_page_in_projection_scope(tmp_path):
    from core.reflection.reflection_store import ReflectionStore

    store = ReflectionStore(str(tmp_path / "canonical" / "reflections.db"))
    independent = tmp_path / "L4-Reflections" / "Reports" / "manual-notes.md"
    independent.parent.mkdir(parents=True)
    independent.write_text("# Manual notes\n", encoding="utf-8")

    ReflectionExporter(str(tmp_path)).export_all(store)

    assert independent.read_text(encoding="utf-8") == "# Manual notes\n"


def test_export_knowledge_updates_writes_wiki_pages(tmp_path):
    """P110: 知识更新建议应被写成 L4-Reflections/KnowledgeUpdates/*.md。"""
    exporter = ReflectionExporter(str(tmp_path))

    record = ReflectionRecord(
        id="rec-110",
        created_at=datetime(2026, 6, 10, 10, 30),
        trigger=ReflectionTrigger.MAJOR_DECISION,
        trigger_event="用户角色转变",
        user_query="我要转向管理岗",
    )
    updates = [
        {
            "dimension": "growth",
            "shift_type": "role_change_to_manager",
            "suggestion": "建议更新职业路径笔记，补充管理相关主题",
            "confidence": 0.85,
            "from_state": "开发者",
            "to_state": "技术负责人",
            "detected_at": datetime(2026, 6, 10, 10, 30).isoformat(),
        }
    ]

    pages = exporter.export_knowledge_updates(record, updates)

    assert len(pages) == 1
    page = pages[0]
    assert page.exists()
    assert "KnowledgeUpdates" in page.parts
    assert "2026-06-10" in page.parts
    content = page.read_text(encoding="utf-8")
    assert "知识更新建议：growth" in content
    assert "role_change_to_manager" in content
    assert "建议更新职业路径笔记" in content
    assert "reflection" in content
    assert "knowledge-update" in content


def test_export_knowledge_updates_empty_returns_empty(tmp_path):
    exporter = ReflectionExporter(str(tmp_path))
    record = ReflectionRecord(
        id="rec-110",
        created_at=datetime(2026, 6, 10, 10, 30),
        trigger=ReflectionTrigger.MAJOR_DECISION,
    )
    assert exporter.export_knowledge_updates(record, []) == []
    assert exporter.export_knowledge_updates(record, None) == []


def test_full_replay_matches_incremental_knowledge_updates_and_removes_stale(tmp_path):
    store = ReflectionStore(str(tmp_path / "canonical" / "reflections.db"))
    record = ReflectionRecord(
        id="replay-knowledge-update",
        created_at=datetime(2026, 6, 10, 10, 30),
        trigger=ReflectionTrigger.MAJOR_DECISION,
        trigger_event="用户角色转变",
        user_query="我要转向管理岗",
    )
    store.save_record(record)
    shift = CognitiveShift(
        dimension="growth",
        shift_type="role_change_to_manager",
        from_state="开发者",
        to_state="技术负责人",
        confidence=0.85,
        evidence=["承担管理职责"],
        first_seen_at=datetime(2026, 6, 1),
        shift_detected_at=datetime(2026, 6, 10, 10, 30),
    )
    store.save_shift(shift, reflection_id=record.id)

    incremental_root = tmp_path / "incremental"
    incremental = ReflectionExporter(str(incremental_root))
    stale_update = {
        **knowledge_update_from_shift(shift),
        "dimension": "stale-dimension",
    }
    incremental.export_knowledge_updates(
        record,
        [knowledge_update_from_shift(shift), stale_update],
    )
    stale_path = next(
        path
        for path in (incremental_root / "L4-Reflections" / "KnowledgeUpdates").rglob("*.md")
        if "_02_" in path.name
    )
    assert stale_path.exists()

    incremental.export_all(store)
    assert not stale_path.exists()

    full_root = tmp_path / "full"
    ReflectionExporter(str(full_root)).export_all(store)

    relative = (
        "L4-Reflections/KnowledgeUpdates/2026-06-10/"
        "replay-knowledge-update_01_growth_role_change_to_manager.md"
    )
    assert (incremental_root / relative).read_bytes() == (full_root / relative).read_bytes()
