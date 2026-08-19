# -*- coding: utf-8 -*-
from __future__ import annotations

import sqlite3

from core.cognitive.policy_patch import PolicyPatchOptions, PolicyPatchStore
from core.reflection.consumers import ReflectionPolicyPatchConsumer
from core.reflection.models import InsightSnapshot, ReflectionRecord, ReflectionTrigger


def _policy_store(tmp_path):
    return PolicyPatchStore(
        options=PolicyPatchOptions(
            database_dir=tmp_path,
            db_path=tmp_path / "policy_patches.db",
            ttl_days=14,
            min_confidence=0.5,
            max_active=5,
        )
    )


def test_reflection_policy_consumer_proposes_high_confidence_patch(tmp_path):
    store = _policy_store(tmp_path)
    consumer = ReflectionPolicyPatchConsumer(policy_store=store, min_confidence=0.8)
    record = ReflectionRecord(
        id="ref-policy-1",
        trigger=ReflectionTrigger.MAJOR_DECISION,
        trigger_event="用户做关键技术取舍",
        mirror_dimensions=["decisions"],
        insight=InsightSnapshot(
            summary="重大决策前需要先列出可回滚路径。",
            key_points=["决策前验证回滚路径"],
            dimensions_involved=["decisions"],
        ),
        internal_validation={"overall_score": 0.9, "passed": True},
    )

    consumer.on_insight_generated(record)

    active = store.active_for(task_type="coding", context="decisions 回滚路径")
    assert len(active) == 1
    assert active[0].source_type == "reflection"
    assert active[0].source_id == "ref-policy-1"
    assert active[0].content == "重大决策前需要先列出可回滚路径。"
    assert "决策前验证回滚路径" not in active[0].trigger


def test_reflection_policy_consumer_records_no_patch_evidence(tmp_path):
    store = _policy_store(tmp_path)
    consumer = ReflectionPolicyPatchConsumer(policy_store=store, min_confidence=0.8)
    record = ReflectionRecord(
        id="ref-policy-low",
        trigger=ReflectionTrigger.MANUAL,
        mirror_dimensions=["growth"],
        insight=InsightSnapshot(
            summary="弱信号，不应生成策略。",
            key_points=[],
            dimensions_involved=["growth"],
        ),
        internal_validation={"overall_score": 0.4, "passed": False},
    )

    consumer.on_insight_generated(record)

    assert store.active_for(task_type="general", context="growth") == []
    with sqlite3.connect(tmp_path / "policy_patches.db") as conn:
        row = conn.execute(
            "SELECT patch_id, outcome, evidence_json FROM policy_patch_feedback"
        ).fetchone()
    assert row[0] == "reflection-no-patch-ref-policy-low"
    assert row[1] == "no_patch"
    assert "confidence_below_policy_threshold" in row[2]
