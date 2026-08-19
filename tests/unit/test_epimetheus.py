import re
import sqlite3
from unittest.mock import patch


def test_should_trigger_ignores_cli_polite_endings():
    from core.kia.epimetheus import AutoRetrospective

    messages = [
        {"role": "assistant", "content": "已经完成了。"},
        {"role": "user", "content": "好的，谢谢"},
    ]

    assert AutoRetrospective().should_trigger(messages) is False
    assert (
        AutoRetrospective().should_trigger([{"role": "user", "content": "复盘一下这次任务"}])
        is True
    )


def test_ending_patterns_document_cli_suppressed_endings():
    from core.kia.epimetheus import AutoRetrospective

    engine = AutoRetrospective()
    samples = ["好的，", "谢谢", "没问题", "ok", "收到"]

    assert len(AutoRetrospective.ENDING_PATTERNS) == len(samples)
    for pattern, sample in zip(AutoRetrospective.ENDING_PATTERNS, samples):
        assert re.search(pattern, sample, re.IGNORECASE)
        assert engine.should_trigger([{"role": "user", "content": sample}]) is False


def test_goal_extraction_separates_expected_and_actual_values():
    from core.kia.epimetheus import AutoRetrospective

    messages = [
        {"role": "user", "content": "目标: 参与人数 100 人，转化率 20%"},
        {"role": "assistant", "content": "执行完成。"},
        {"role": "user", "content": "实际: 参与人数 70 人，转化率 12%"},
    ]

    result = AutoRetrospective().generate("marketing", "event", messages, [])

    assert result.expected_goals["participants"] == "100"
    assert result.actual_results["participants"] == "70"
    assert any(gap.severity == "medium" for gap in result.gaps)


def test_blindspot_degradation_is_visible_without_profile():
    from core.kia.epimetheus import AutoRetrospective

    result = AutoRetrospective().generate(
        "coding",
        "refactor",
        [
            {"role": "user", "content": "目标: 完成重构"},
            {"role": "user", "content": "实际: 完成核心路径"},
        ],
        [],
    )

    assert result.blindspot_focus
    assert result.blindspot_focus[0].blindspot_type == "blindspot_profile_unavailable"
    assert result.blindspot_focus[0].was_triggered is False


def test_create_recap_todo_uses_forced_retrospective_store(
    tmp_path,
):
    from core.kia.epimetheus import AutoRetrospective, GoalComparison, RetrospectiveResult

    db_path = tmp_path / "recap.db"
    result = RetrospectiveResult(
        task_type="coding",
        subtype="bugfix",
        version=0,
        gaps=[GoalComparison("budget", "100", "50", "显著偏差", "high")],
        summary="存在 1 个显著偏差",
    )

    with (
        patch("core.app.forced_retrospective.get_config") as mock_cfg,
        patch("core.app.forced_retrospective.REMINDERS_DIR", tmp_path / "08-Reminders"),
    ):
        mock_cfg.return_value.wiki_dir = tmp_path
        task_id = AutoRetrospective(recap_db_path=db_path).create_recap_todo(result)

    assert task_id.startswith("system-recap-")
    with sqlite3.connect(db_path) as conn:
        row = conn.execute(
            "SELECT severity, topic, status, context, suggested_points FROM recap_tasks WHERE task_id=?",  # noqa: E501
            (task_id,),
        ).fetchone()
    assert row[0] == "high"
    assert row[1] == "coding/bugfix：预算/资源消耗偏差（预期 100，实际 50）"
    assert row[2] == "pending"
    assert "budget" in row[3] and "显著偏差" in row[3]
    assert "偏差项：预算/资源消耗（budget）" in row[3]
    assert "预期值：100" in row[3]
    assert "实际值：50" in row[3]
    assert "这次复盘要回答" in row[3]
    assert row[4] and "偏差" in row[4]
    assert "为什么预算/资源消耗从预期 100 变成实际 50" in row[4]


def test_write_dashboard_creates_visible_recap_board(tmp_path):
    from core.kia.epimetheus import AutoRetrospective

    path = AutoRetrospective(wiki_base=tmp_path).write_dashboard(
        [
            {
                "time": "2026-05-27",
                "source": "Hermes",
                "summary": "重构蓝图联动更新",
                "status": "pending",
            }
        ]
    )

    content = path.read_text(encoding="utf-8")
    assert "hermes_type: dashboard" in content
    assert "重构蓝图联动更新" in content
