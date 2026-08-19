# -*- coding: utf-8 -*-
"""Tests for core.persona.behavior_tracker."""

from datetime import datetime, timedelta


from core.persona.behavior_tracker import BehaviorPromptTracker, _extract_strategies


def test_extract_strategies_from_prompt():
    """从提示文本中提取策略标签。"""
    prompt = (
        "\n[Persona-Driven Behavior]\n"
        "- 用户专注深度高：提供结构化、层次化的深度回复\n"
        "- 用户偏抽象思维：先说原理/框架，再用案例佐证\n"
        "- 用户重视正确性：确保信息准确\n"
        "- 用户追求完美：提供详尽、完整的方案\n"
    )
    strategies = _extract_strategies(prompt)
    assert "focus_depth_high" in strategies
    assert "abstraction_high" in strategies
    assert "correctness_first" in strategies
    assert "perfection_oriented" in strategies


def test_extract_strategies_empty_prompt():
    """空 prompt 返回空列表。"""
    assert _extract_strategies("") == []
    assert _extract_strategies(None) == []


def test_track_writes_record(tmp_path):
    """track 写入数据库记录。"""
    db_path = tmp_path / "user_signals.db"
    tracker = BehaviorPromptTracker(db_path=db_path)

    prompt = "[Persona-Driven Behavior]\n- 用户专注深度高：...\n- 用户偏抽象思维：..."
    ok = tracker.track(
        agent="claude",
        source="preflight",
        prompt_text=prompt,
        ab_test_group="treatment",
    )
    assert ok is True

    metrics = tracker.get_metrics(days=1)
    assert metrics["total_calls"] == 1
    assert metrics["by_agent"].get("claude") == 1
    assert metrics["by_source"].get("preflight") == 1
    assert metrics["ab_test"].get("treatment") == 1
    assert "focus_depth_high" in metrics["by_strategy"]
    assert "abstraction_high" in metrics["by_strategy"]


def test_get_metrics_filters_by_days(tmp_path):
    """get_metrics 只返回指定天数内的记录。"""
    db_path = tmp_path / "user_signals.db"
    tracker = BehaviorPromptTracker(db_path=db_path)

    old_ts = (datetime.now() - timedelta(days=60)).isoformat()
    tracker.track(
        agent="claude",
        source="preflight",
        prompt_text="- 用户专注深度高：...",
        timestamp=old_ts,
    )
    tracker.track(
        agent="claude",
        source="preflight",
        prompt_text="- 用户专注深度高：...",
    )

    metrics = tracker.get_metrics(days=30)
    assert metrics["total_calls"] == 1


def test_track_isolation_on_failure(tmp_path, monkeypatch):
    """写入失败时不抛异常。"""
    import sqlite3

    class FakeConn:
        def execute(self, *args, **kwargs):
            raise sqlite3.OperationalError("disk I/O error")

        def commit(self):
            pass

    monkeypatch.setattr(sqlite3, "connect", lambda path, timeout=None: FakeConn())

    db_path = tmp_path / "user_signals.db"
    tracker = BehaviorPromptTracker(db_path=db_path)

    ok = tracker.track(agent="claude", source="preflight", prompt_text="test")
    assert ok is False
