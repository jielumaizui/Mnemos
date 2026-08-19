"""
Tests for core.app.application_hub

Covers: AppOutput, PushPenaltyTracker, ApplicationHub submit/rate-limit/dedup.
"""

import sqlite3
import time
from pathlib import Path

import pytest

from core.app.application_hub import (
    AppOutput,
    PushPenaltyTracker,
    ApplicationHub,
    BLINDSPOT_COOLDOWN_SEC,
    DEDUP_WINDOW_SEC,
    RATE_LIMITS,
)


class TestAppOutput:
    def test_defaults(self):
        o = AppOutput(output_type="search", priority=0, knowledge_id="k1", content="test")
        assert o.timestamp > 0
        assert "search" in o.explain()

    def test_explain_truncates_context(self):
        o = AppOutput(
            output_type="push",
            priority=2,
            knowledge_id="k1",
            content="x",
            context="a" * 100,
        )
        assert len(o.explain()) < 100


class TestPushPenaltyTracker:
    @pytest.fixture
    def tracker(self, tmp_path):
        db = tmp_path / "penalty.db"
        return PushPenaltyTracker(db_path=str(db))

    def test_init_creates_table(self, tracker, tmp_path):
        db = tmp_path / "penalty.db"
        with sqlite3.connect(str(db)) as conn:
            cursor = conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
            assert "push_penalties" in {r[0] for r in cursor}

    def test_record_ignore_increments(self, tracker):
        m1 = tracker.record_ignore("topic_a")
        assert m1 >= 1.0
        m2 = tracker.record_ignore("topic_a")
        assert m2 >= m1

    def test_penalty_levels(self, tracker):
        """测试惩罚等级递增"""
        m1 = tracker.record_ignore("t")
        m2 = tracker.record_ignore("t")
        m3 = tracker.record_ignore("t")
        m4 = tracker.record_ignore("t")
        assert m1 == 1.5
        assert m2 == 2.0
        assert m3 == 6.0
        assert m4 == 6.0  # 最大 6x

    def test_feedback_event_id_makes_penalty_update_idempotent(self, tracker):
        first = tracker.record_ignore("topic-event", feedback_event_id="feedback-1")
        repeated = tracker.record_ignore("topic-event", feedback_event_id="feedback-1")

        assert first == 1.5
        assert repeated == 1.5
        with sqlite3.connect(str(tracker.DB_PATH)) as conn:
            assert conn.execute(
                "SELECT ignore_count FROM push_penalties WHERE topic='topic-event'"
            ).fetchone() == (1,)
            assert conn.execute(
                "SELECT COUNT(*) FROM push_penalty_feedback_events"
            ).fetchone() == (1,)

    def test_record_accept_resets(self, tracker):
        tracker.record_ignore("topic_b")
        tracker.record_accept("topic_b")
        assert tracker.is_in_cooldown("topic_b") is False

    def test_is_in_cooldown_true(self, tracker):
        tracker.record_ignore("topic_c")
        assert tracker.is_in_cooldown("topic_c") is True

    def test_is_in_cooldown_expired(self, tracker):
        tracker.record_ignore("topic_d")
        # 手动将 cooldown_until 设为过去
        with sqlite3.connect(str(tracker.DB_PATH)) as conn:
            conn.execute(
                "UPDATE push_penalties SET cooldown_until = ? WHERE topic = ?",
                ("2000-01-01T00:00:00", "topic_d"),
            )
            conn.commit()
        assert tracker.is_in_cooldown("topic_d") is False

    def test_is_in_cooldown_no_record(self, tracker):
        assert tracker.is_in_cooldown("no_such_topic") is False


class TestApplicationHub:
    @pytest.fixture
    def hub(self, tmp_path):
        db = tmp_path / "hub.db"
        return ApplicationHub(db_path=str(db))

    def test_submit_empty(self, hub):
        assert hub.submit([]) == []

    def test_submit_dedup(self, hub):
        o1 = AppOutput(output_type="search", priority=0, knowledge_id="k1", content="c1")
        result = hub.submit([o1])
        assert len(result) == 1
        # 再次提交相同 knowledge_id 应在去重窗口内被过滤
        result2 = hub.submit([o1])
        assert result2 == []

    def test_submit_priority_sorting(self, hub):
        o1 = AppOutput(output_type="push", priority=2, knowledge_id="k1", content="c1")
        o2 = AppOutput(output_type="search", priority=0, knowledge_id="k2", content="c2")
        result = hub.submit([o1, o2])
        assert result[0].priority <= result[-1].priority

    def test_submit_rate_limit_search_unlimited(self, hub):
        """search 类型不受速率限制"""
        outputs = [
            AppOutput(output_type="search", priority=0, knowledge_id=f"k{i}", content="c")
            for i in range(10)
        ]
        result = hub.submit(outputs)
        assert len(result) == 10

    def test_submit_rate_limit_blindspot_daily(self, hub, monkeypatch):
        """blind_spot 每日上限由 ApplicationHub 兜底保护。"""
        monkeypatch.setattr("core.app.application_hub.MIN_INTERVAL_SEC", 0.0)
        outputs = [
            AppOutput(output_type="blind_spot", priority=1, knowledge_id=f"k{i}", content="c")
            for i in range(RATE_LIMITS["blind_spot"]["max_per_day"])
        ]
        assert len(hub.submit(outputs)) == RATE_LIMITS["blind_spot"]["max_per_day"]

        extra = AppOutput(output_type="blind_spot", priority=1, knowledge_id="k-extra", content="c")
        assert hub.submit([extra]) == []

    def test_blindspot_cooldown_policy_is_delegated(self):
        """ApplicationHub exposes but does not enforce the blindspot topic cooldown."""
        limits = RATE_LIMITS["blind_spot"]
        assert limits["cooldown_sec"] == 0
        assert limits["delegated_cooldown_sec"] == BLINDSPOT_COOLDOWN_SEC
        assert BLINDSPOT_COOLDOWN_SEC == 24 * 60 * 60

    def test_submit_rate_limit_push_cooldown(self, hub):
        """predictive_push 受冷却限制"""
        o1 = AppOutput(output_type="predictive_push", priority=2, knowledge_id="k1", content="c1")
        result1 = hub.submit([o1])
        assert len(result1) == 1
        # 立即再次提交应被冷却
        o2 = AppOutput(output_type="predictive_push", priority=2, knowledge_id="k2", content="c2")
        result2 = hub.submit([o2])
        assert len(result2) == 0

    def test_submit_rate_limit_push_penalty(self, hub):
        """predictive_push 受惩罚冷却限制"""
        hub.penalty_tracker.record_ignore("topic_a")
        o = AppOutput(
            output_type="predictive_push",
            priority=2,
            knowledge_id="topic_a:sub",
            content="c",
        )
        result = hub.submit([o])
        assert len(result) == 0

    def test_submit_global_interval(self, hub):
        """非 search 类型受全局 1 秒间隔限制"""
        o1 = AppOutput(output_type="push", priority=2, knowledge_id="k1", content="c1")
        o2 = AppOutput(output_type="push", priority=2, knowledge_id="k2", content="c2")
        result = hub.submit([o1, o2])
        # 第二个应被间隔限制
        assert len(result) == 1

    def test_search_only_types_not_output(self, hub):
        """evolution_alert 和 dispute 不主动输出"""
        o1 = AppOutput(output_type="evolution_alert", priority=3, knowledge_id="k1", content="c")
        o2 = AppOutput(output_type="dispute", priority=4, knowledge_id="k2", content="c")
        result = hub.submit([o1, o2])
        assert len(result) == 0

    def test_dedup_after_window_expires(self, hub):
        """去重窗口过期后应允许重复"""
        o = AppOutput(output_type="search", priority=0, knowledge_id="old_k", content="c")
        # 伪造一条旧记录
        with sqlite3.connect(str(hub.DB_PATH)) as conn:
            conn.execute(
                "INSERT INTO output_history (output_type, knowledge_id, timestamp) VALUES (?, ?, ?)",  # noqa: E501
                ("search", "old_k", time.time() - DEDUP_WINDOW_SEC - 1),
            )
            conn.commit()
        result = hub.submit([o])
        assert len(result) == 1

    def test_max_per_batch_limits_predictive_push(self, hub, monkeypatch):
        """predictive_push 单批上限来自 delivery profile。"""
        class _Policy:
            per_task_total = 2

        outputs = [
            AppOutput(
                output_type="predictive_push", priority=2, knowledge_id=f"topic{i}:sub", content="c"
            )
            for i in range(5)
        ]
        # 绕过所有其他速率限制，只验证单批上限
        monkeypatch.setattr(
            "core.app.application_hub.ApplicationHub._delivery_policy",
            staticmethod(lambda: _Policy()),
        )
        monkeypatch.setattr(hub, "_check_rate_limit", lambda output: True)
        monkeypatch.setattr("core.app.application_hub.MIN_INTERVAL_SEC", 0.0)
        result = hub.submit(outputs)
        assert len(result) == 2
        #  knowledge_id 应各不相同
        assert len({o.knowledge_id for o in result}) == 2

    def test_global_interval_persists(self, tmp_path):
        """全局 1s 间隔应持久化到 SQLite，进程重启后仍生效"""
        db = tmp_path / "hub.db"
        hub1 = ApplicationHub(db_path=str(db))
        o = AppOutput(output_type="push", priority=2, knowledge_id="k1", content="c")
        hub1.submit([o])

        # 模拟进程重启，重新初始化 ApplicationHub
        hub2 = ApplicationHub(db_path=str(db))
        o2 = AppOutput(output_type="push", priority=2, knowledge_id="k2", content="c")
        result = hub2.submit([o2])
        # 1 秒内重启后立即提交应被全局间隔限制
        assert len(result) == 0

    def test_unified_penalty_db_path(self, hub):
        """ApplicationHub 应使用 push_penalty.db 作为惩罚库"""
        expected = hub.DB_PATH.with_name("push_penalty.db")
        assert Path(hub.penalty_tracker.DB_PATH) == expected
