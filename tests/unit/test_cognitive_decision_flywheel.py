from __future__ import annotations

import sqlite3
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

from core.kia.ixion import CognitiveDecisionFlywheel, FlywheelInsight


def test_methodology_page_becomes_cognitive_decision_asset(tmp_path: Path) -> None:
    page = tmp_path / "03-Tech" / "methodology.md"
    page.parent.mkdir(parents=True)
    page.write_text(
        "---\n类型: 方法论\n置信度: 0.9\n触发场景:\n  - code review\n---\n"
        "\n# 如何做代码审查\n\n1. 先看风险\n2. 再看测试\n\n## 边界\n不适用于纯格式调整。\n",
        encoding="utf-8",
    )
    flywheel = CognitiveDecisionFlywheel(
        wiki_base=str(tmp_path), db_path=str(tmp_path / "flywheel.db")
    )
    for _ in range(5):
        flywheel.log_wiki_usage(str(page), "read")

    insight = flywheel.analyze_wiki_for_cognitive_decision(page)

    assert insight is not None
    assert insight.schema_version == "cognitive_decision_asset.v1"
    assert insight.direction == "wiki_to_cognitive_decision"
    assert insight.asset_type in {"methodology", "verification_recipe", "decision_heuristic"}
    assert insight.automation_derivative_allowed is False
    assert "不适用" in " ".join(insight.failure_modes) or "边界" in " ".join(
        insight.failure_modes
    )


def test_behavior_pattern_creates_asset_not_skill(tmp_path: Path) -> None:
    flywheel = CognitiveDecisionFlywheel(
        wiki_base=str(tmp_path), db_path=str(tmp_path / "flywheel.db")
    )
    with sqlite3.connect(tmp_path / "flywheel.db") as conn:
        flywheel.behavior_generator._init_task_history(conn)
        for _ in range(3):
            conn.execute(
                "INSERT INTO task_history (task_type, subtype, completed_at) VALUES (?, ?, ?)",
                ("coding", "review", datetime.now().isoformat()),
            )
        conn.commit()

    insight = flywheel.behavior_generator.analyze()[0]
    executed = flywheel.execute_insights(
        {
            "wiki_to_cognitive_decision": [],
            "behavior_to_cognitive_decision": [insight],
            "skill_to_cognitive_decision": [],
        }
    )

    assert executed["count"] == 1
    assets = flywheel.list_cognitive_decision_assets()
    assert len(assets) == 1
    assert assets[0].schema_version == "cognitive_decision_asset.v1"
    assert assets[0].asset_type == "verification_recipe"
    assert assets[0].automation_derivative_allowed is False
    assert flywheel.list_skills() == []


def test_automation_skill_derives_only_when_asset_allows_it(
    tmp_path: Path,
) -> None:
    page = tmp_path / "method.md"
    page.write_text("---\n---\n\n# 方法\n", encoding="utf-8")
    flywheel = CognitiveDecisionFlywheel(
        wiki_base=str(tmp_path), db_path=str(tmp_path / "flywheel.db")
    )
    insight = FlywheelInsight(
        direction="wiki_to_cognitive_decision",
        source=str(page),
        target="稳定流程认知决策资产",
        confidence=0.95,
        reason="资产已验证且边界清楚",
        auto_applicable=True,
        automation_derivative_allowed=True,
        applicability=["stable flow"],
    )

    flywheel.execute_insights(
        {
            "wiki_to_cognitive_decision": [insight],
            "behavior_to_cognitive_decision": [],
            "skill_to_cognitive_decision": [],
        }
    )

    assert len(flywheel.list_cognitive_decision_assets()) == 1
    skills = flywheel.list_skills()
    assert len(skills) == 1
    assert skills[0].generation_source == "cognitive_decision_asset"


def test_flywheel_reads_thresholds_from_config(tmp_path: Path, monkeypatch) -> None:
    import core.kia.ixion as ixion_mod

    fake_config = SimpleNamespace(
        wiki_dir=tmp_path,
        get=lambda key, default=None: {
            "skill.cognitive_decision_flywheel": {
                "min_occurrences": 2,
                "time_window_days": 14,
            }
        }.get(key, default),
    )
    monkeypatch.setattr(ixion_mod, "get_config", lambda: fake_config)
    flywheel = CognitiveDecisionFlywheel(db_path=str(tmp_path / "flywheel.db"))
    with sqlite3.connect(tmp_path / "flywheel.db") as conn:
        flywheel.behavior_generator._init_task_history(conn)
        for _ in range(2):
            conn.execute(
                "INSERT INTO task_history (task_type, subtype, completed_at) VALUES (?, ?, ?)",
                ("docs", "review", datetime.now().isoformat()),
            )
        conn.commit()

    insights = flywheel.behavior_generator.analyze()

    assert len(insights) == 1
    assert "近14天" in insights[0].reason
