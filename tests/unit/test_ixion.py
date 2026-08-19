"""
Ixion — Cognitive Decision Flywheel 单元测试

覆盖公共 API：
- 数据类: AutomationSkillRecord, SkillUsageLog, FlywheelInsight, PersonaSkillGap,
  SkillPath, SkillVerificationTask
- BehaviorDrivenSkillGenerator: analyze, _init_task_history
- PersonaDrivenSkillEngine: analyze_skill_gaps, generate_skill_paths,
  generate_tasks_by_values, get_flywheel_params, generate_verification_tasks,
  format_persona_insights
- CognitiveDecisionFlywheel: enable/disable/configure/handle_event,
  analyze_wiki_for_cognitive_decision, scan_wiki_for_cognitive_decision_assets, log_skill_usage,
  analyze_skill_for_wiki, log_wiki_usage, create_skill/get_skill/list_skills,
  run_cycle, cleanup_stale_skills, _archive_skill, record_task_completed,
  update_persona, generate_cycle_report, _fallback_from_metis,
  _run_persona_driven_cycle
- 便捷函数: run_flywheel, get_skill_gaps, get_personalized_skill_paths, get_verification_tasks
"""

from __future__ import annotations

import json
import sqlite3
from types import SimpleNamespace
from dataclasses import asdict
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from core.kia.ixion import (
    BehaviorDrivenSkillGenerator,
    FlywheelInsight,
    PersonaDrivenSkillEngine,
    PersonaSkillGap,
    SkillPath,
    AutomationSkillRecord,
    SkillUsageLog,
    SkillVerificationTask,
    CognitiveDecisionFlywheel,
    get_personalized_skill_paths,
    get_skill_gaps,
    get_verification_tasks,
    run_flywheel,
)
from core.trust.proposal_queue import ProposalQueue

# ============================================================
# Fixtures
# ============================================================


@pytest.fixture
def tmp_db_path(tmp_path: Path) -> Path:
    """提供独立的临时数据库路径。"""
    return tmp_path / "flywheel.db"


@pytest.fixture
def tmp_wiki_dir(tmp_path: Path) -> Path:
    """提供独立的临时 Wiki 目录。"""
    wiki = tmp_path / "wiki"
    wiki.mkdir()
    return wiki


@pytest.fixture
def flywheel(tmp_wiki_dir: Path, tmp_db_path: Path, monkeypatch) -> CognitiveDecisionFlywheel:
    """提供已初始化的 CognitiveDecisionFlywheel 实例，使用临时目录和数据库。"""
    import core.config as _config_mod

    fake_cfg = MagicMock()
    fake_cfg.wiki_dir = tmp_wiki_dir
    fake_cfg.data_dir = tmp_wiki_dir.parent / "data"
    monkeypatch.setattr(_config_mod, "get_config", lambda: fake_cfg)

    fw = CognitiveDecisionFlywheel(wiki_base=str(tmp_wiki_dir), db_path=str(tmp_db_path))
    return fw


@pytest.fixture
def sample_skill() -> AutomationSkillRecord:
    """提供一个示例 AutomationSkillRecord。"""
    return AutomationSkillRecord(
        skill_name="test-skill",
        description="A test skill",
        trigger_conditions=["on demand"],
        input_template="{query}",
        expected_output="result",
        source_wiki_pages=["wiki/test.md"],
        status="active",
        version=1,
        generation_source="manual",
    )


@pytest.fixture
def fixed_now() -> datetime:
    """固定当前时间。"""
    return datetime(2026, 6, 7, 12, 0, 0)


# ============================================================
# 数据类
# ============================================================


def test_skill_record_defaults() -> None:
    """AutomationSkillRecord 默认值应正确。"""
    sr = AutomationSkillRecord(skill_name="foo")
    assert sr.skill_name == "foo"
    assert sr.description == ""
    assert sr.trigger_conditions == []
    assert sr.input_template == ""
    assert sr.expected_output == ""
    assert sr.source_wiki_pages == []
    assert sr.usage_count == 0
    assert sr.success_count == 0
    assert sr.failure_count == 0
    assert sr.status == "proposed"
    assert sr.version == 1
    assert sr.generation_source == ""
    assert sr.parent_version == 0
    assert sr.deviation_log == []


def test_skill_usage_log_defaults() -> None:
    """SkillUsageLog 默认值应正确。"""
    log = SkillUsageLog()
    assert log.log_id == 0
    assert log.skill_name == ""
    assert log.status == ""
    assert log.new_scenario is False
    assert log.user_marked is False
    assert log.generated_wiki == ""


def test_flywheel_insight_defaults() -> None:
    """FlywheelInsight 默认值应正确。"""
    fi = FlywheelInsight(
        direction="wiki_to_cognitive_decision",
        source="s",
        target="t",
        confidence=0.5,
        reason="r",
    )
    assert fi.suggested_action == ""
    assert fi.auto_applicable is False


def test_persona_skill_gap_defaults() -> None:
    """PersonaSkillGap 默认值应正确。"""
    gap = PersonaSkillGap(
        dimension="抽象",
        current_score=0.2,
        target_score=0.5,
        gap_severity="critical",
        recommended_skill_category="模式识别",
        rationale="r",
    )
    assert gap.related_wiki_pages == []


def test_skill_path_structure() -> None:
    """SkillPath 应能正确存储阶段信息。"""
    path = SkillPath(
        path_id="p1",
        title="t",
        description="d",
        stages=[{"name": "s1", "type": "t1"}],
        cognitive_style="deductive",
        estimated_duration="1周",
        priority="high",
    )
    assert path.stages[0]["name"] == "s1"


def test_skill_verification_task_defaults() -> None:
    """SkillVerificationTask 默认状态为 pending。"""
    task = SkillVerificationTask(
        task_id="t1",
        task_type="framing_challenge",
        description="d",
        related_skill="s",
        related_blindspot_type="framing",
        verification_method="m",
        expected_outcome="o",
    )
    assert task.status == "pending"


# ============================================================
# BehaviorDrivenSkillGenerator
# ============================================================


def test_behavior_generator_analyze_no_db(tmp_path: Path) -> None:
    """数据库不存在时应返回空列表。"""
    gen = BehaviorDrivenSkillGenerator(tmp_path / "nonexistent.db")
    assert gen.analyze() == []


def test_behavior_generator_analyze_finds_repeated_tasks(
    tmp_db_path: Path, fixed_now: datetime
) -> None:
    """应识别出 30 天内重复完成的任务。"""
    gen = BehaviorDrivenSkillGenerator(tmp_db_path)
    # 先创建数据库和表
    with sqlite3.connect(str(tmp_db_path)) as conn:
        gen._init_task_history(conn)
        for _ in range(3):
            conn.execute(
                "INSERT INTO task_history (task_type, subtype, completed_at) VALUES (?, ?, ?)",
                ("coding", "review", fixed_now.isoformat()),
            )
        conn.commit()

    with patch("core.kia.cognitive_decision_assets.datetime") as mock_dt:
        mock_dt.now.return_value = fixed_now
        mock_dt.fromisoformat = datetime.fromisoformat
        mock_dt.timedelta = timedelta
        insights = gen.analyze()

    assert len(insights) == 1
    assert insights[0].direction == "behavior_to_cognitive_decision"
    assert "coding/review" in insights[0].source
    assert insights[0].confidence == 0.75
    assert insights[0].asset_type == "verification_recipe"
    assert insights[0].auto_applicable is True


def test_behavior_generator_init_task_history_creates_table(tmp_db_path: Path) -> None:
    """_init_task_history 应创建 task_history 表。"""
    with sqlite3.connect(str(tmp_db_path)) as conn:
        BehaviorDrivenSkillGenerator._init_task_history(conn)
        tables = {
            row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        assert "task_history" in tables


# ============================================================
# PersonaDrivenSkillEngine (无 persona 可用)
# ============================================================


def test_persona_engine_no_persona_returns_empty() -> None:
    """未提供 persona 时所有分析应返回空/默认值。"""
    engine = PersonaDrivenSkillEngine(persona=None)
    assert engine.analyze_skill_gaps() == []
    assert engine.generate_skill_paths() == []
    assert engine.generate_tasks_by_values(["s1"]) == []
    assert (
        engine.get_flywheel_params() == PersonaDrivenSkillEngine.ENERGY_TO_FLYWHEEL_PARAMS["mixed"]
    )
    assert engine.generate_verification_tasks(["s1"]) == []


def test_persona_engine_format_insights_empty() -> None:
    """format_persona_insights 在空数据时应生成友好提示。"""
    engine = PersonaDrivenSkillEngine(persona=None)
    text = engine.format_persona_insights([], [], [], [])
    assert "能力短板识别" in text
    assert "当前无显著能力短板" in text
    assert "个性化学习路径" in text
    assert "暂无推荐路径" in text


# ============================================================
# CognitiveDecisionFlywheel — 初始化与 PluggableModule 接口
# ============================================================


def test_flywheel_init_creates_db(tmp_wiki_dir: Path, tmp_db_path: Path, monkeypatch) -> None:
    """初始化时应自动创建数据库和表。"""
    import core.config as _config_mod

    fake_cfg = MagicMock()
    fake_cfg.wiki_dir = tmp_wiki_dir
    monkeypatch.setattr(_config_mod, "get_config", lambda: fake_cfg)

    assert not tmp_db_path.exists()
    _ = CognitiveDecisionFlywheel(wiki_base=str(tmp_wiki_dir), db_path=str(tmp_db_path))
    assert tmp_db_path.exists()

    with sqlite3.connect(str(tmp_db_path)) as conn:
        tables = {
            row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        assert "skills" in tables
        assert "skill_usage_logs" in tables
        assert "wiki_usage_logs" in tables
        assert "skill_paths" in tables
        assert "skill_verification_tasks" in tables
        assert "persona_flywheel_logs" in tables
        assert "skill_versions" in tables
        assert "task_history" in tables


def test_flywheel_enable_disable(flywheel: CognitiveDecisionFlywheel) -> None:
    """enable/disable 应切换 _enabled 状态。"""
    assert flywheel._enabled is True
    flywheel.disable()
    assert flywheel._enabled is False
    flywheel.enable()
    assert flywheel._enabled is True


def test_flywheel_configure_updates_thresholds(flywheel: CognitiveDecisionFlywheel) -> None:
    """configure 应更新信号阈值。"""
    flywheel.configure({"wiki_to_cognitive_decision_signals": {"min_usage_count": 10}})
    assert flywheel.WIKI_TO_COGNITIVE_DECISION_SIGNALS["min_usage_count"] == 10
    flywheel.configure({"skill_to_cognitive_decision_signals": {"failure_rate_threshold": 0.5}})
    assert flywheel.SKILL_TO_COGNITIVE_DECISION_SIGNALS["failure_rate_threshold"] == 0.5


def test_flywheel_handle_event_disabled_does_nothing(flywheel: CognitiveDecisionFlywheel) -> None:
    """disabled 状态下 handle_event 不应执行操作。"""
    flywheel.disable()
    # 不应抛出异常
    flywheel.handle_event("task_completed", {})
    flywheel.handle_event("page_accessed", {"page_path": "foo.md"})


def test_flywheel_handle_event_page_accessed(flywheel: CognitiveDecisionFlywheel) -> None:
    """handle_event page_accessed 应记录 Wiki 使用。"""
    flywheel.handle_event("page_accessed", {"page_path": "foo.md", "access_type": "read"})
    assert flywheel._get_wiki_usage("foo.md") == 1


# ============================================================
# CognitiveDecisionFlywheel — Wiki → Cognitive Decision Asset
# ============================================================


def test_analyze_wiki_for_cognitive_decision_nonexistent_returns_none(
    flywheel: CognitiveDecisionFlywheel,
) -> None:
    """页面不存在时返回 None。"""
    assert flywheel.analyze_wiki_for_cognitive_decision(Path("/nonexistent.md")) is None


def test_analyze_wiki_for_cognitive_decision_low_confidence_returns_none(
    flywheel: CognitiveDecisionFlywheel, tmp_wiki_dir: Path
) -> None:
    """信号不足时返回 None。"""
    page = tmp_wiki_dir / "plain.md"
    page.write_text("# Hello\n\nSome text without steps.", encoding="utf-8")
    assert flywheel.analyze_wiki_for_cognitive_decision(page) is None


def test_analyze_wiki_for_cognitive_decision_high_confidence(
    flywheel: CognitiveDecisionFlywheel, tmp_wiki_dir: Path
) -> None:
    """多信号叠加达到阈值时应返回 FlywheelInsight。"""
    page = tmp_wiki_dir / "guide.md"
    content = """---
类型: 方法论
置信度: 0.8
触发场景:
  - 每日复盘
---

# 如何写测试步骤流程指南

第一步：准备环境
第二步：编写用例

1. 打开编辑器
2. 写代码
"""
    page.write_text(content, encoding="utf-8")
    # 伪造使用次数达到阈值
    for _ in range(5):
        flywheel.log_wiki_usage(str(page), "read")

    insight = flywheel.analyze_wiki_for_cognitive_decision(page)
    assert insight is not None
    assert insight.direction == "wiki_to_cognitive_decision"
    assert insight.confidence >= 0.5
    assert "认知决策资产" in insight.target
    assert insight.suggested_action != ""


def test_scan_wiki_for_cognitive_decision_assets_sorts_by_confidence(
    flywheel: CognitiveDecisionFlywheel, tmp_wiki_dir: Path
) -> None:
    """scan_wiki_for_cognitive_decision_assets 应按置信度降序排列。"""
    # 创建两个页面，一个高置信度一个低置信度
    high = tmp_wiki_dir / "high.md"
    high.write_text(
        "---\n类型: 方法论\n置信度: 0.9\n触发场景:\n  - s\n---\n\n# 流程步骤\n\n第一步：做某事\n",
        encoding="utf-8",
    )
    for _ in range(10):
        flywheel.log_wiki_usage(str(high), "read")

    low = tmp_wiki_dir / "low.md"
    low.write_text(
        "---\n类型: 经验法则\n置信度: 0.6\n触发场景:\n  - s\n---\n\n# 检查清单\n\n1. a\n",
        encoding="utf-8",
    )
    for _ in range(5):
        flywheel.log_wiki_usage(str(low), "read")

    insights = flywheel.scan_wiki_for_cognitive_decision_assets()
    assert len(insights) >= 2
    assert insights[0].confidence >= insights[1].confidence


def test_scan_wiki_excludes_hidden_dirs(flywheel: CognitiveDecisionFlywheel, tmp_wiki_dir: Path) -> None:
    """应排除 .git, .obsidian, .kg 等目录。"""
    hidden = tmp_wiki_dir / ".obsidian"
    hidden.mkdir()
    (hidden / "note.md").write_text("---\n类型: 方法论\n---\n\n# 步骤\n\n1. a\n", encoding="utf-8")
    insights = flywheel.scan_wiki_for_cognitive_decision_assets()
    for i in insights:
        assert ".obsidian" not in i.source


def test_suggest_skill_name_strips_question_words(flywheel: CognitiveDecisionFlywheel) -> None:
    """_suggest_skill_name 应去掉疑问词并添加助手后缀。"""
    assert flywheel._suggest_skill_name("如何写测试") == "写测试助手"
    assert flywheel._suggest_skill_name("什么是Python") == "Python助手"
    assert flywheel._suggest_skill_name("已有助手") == "已有助手"


def test_extract_frontmatter(flywheel: CognitiveDecisionFlywheel) -> None:
    """_extract_frontmatter 应正确解析 YAML frontmatter。"""
    content = "---\nfoo: bar\nlist:\n  - a\n  - b\n---\n\n# Title\n"
    fm = flywheel._extract_frontmatter(content)
    assert fm.get("foo") == "bar"


def test_extract_frontmatter_no_frontmatter(flywheel: CognitiveDecisionFlywheel) -> None:
    """无 frontmatter 时应返回空字典。"""
    assert flywheel._extract_frontmatter("# Title\n\nBody") == {}


def test_extract_body(flywheel: CognitiveDecisionFlywheel) -> None:
    """_extract_body 应正确提取正文。"""
    content = "---\nfoo: bar\n---\n\n# Title\n\nBody"
    assert "# Title" in flywheel._extract_body(content)
    assert "---" not in flywheel._extract_body(content)


def test_extract_title(flywheel: CognitiveDecisionFlywheel) -> None:
    """_extract_title 应提取第一个 H1。"""
    assert flywheel._extract_title("# Hello World\n\nBody") == "Hello World"
    assert flywheel._extract_title("No title") == ""


# ============================================================
# CognitiveDecisionFlywheel — Skill → Wiki
# ============================================================


def test_log_skill_usage_updates_stats(
    flywheel: CognitiveDecisionFlywheel, sample_skill: AutomationSkillRecord
) -> None:
    """log_skill_usage 应插入日志并更新统计。"""
    flywheel.create_skill(sample_skill)
    log_id = flywheel.log_skill_usage(
        "test-skill", input_data="in", output_data="out", status="success"
    )
    assert isinstance(log_id, int)
    assert log_id > 0

    skill = flywheel.get_skill("test-skill")
    assert skill.usage_count == 1
    assert skill.success_count == 1
    assert skill.failure_count == 0


def test_log_skill_usage_persists_generated_wiki_contract(
    flywheel: CognitiveDecisionFlywheel, sample_skill: AutomationSkillRecord
) -> None:
    """generated_wiki 应作为 SkillUsageLog DTO 和 DB 字段保留。"""
    log = SkillUsageLog(skill_name="test-skill", generated_wiki="04-Skills/test.md")
    assert asdict(log)["generated_wiki"] == "04-Skills/test.md"

    flywheel.create_skill(sample_skill)
    log_id = flywheel.log_skill_usage(
        "test-skill",
        status="success",
        generated_wiki="04-Skills/test.md",
    )

    with sqlite3.connect(str(flywheel.db_path)) as conn:
        row = conn.execute(
            "SELECT generated_wiki FROM skill_usage_logs WHERE log_id=?",
            (log_id,),
        ).fetchone()

    assert row[0] == "04-Skills/test.md"


def test_analyze_skill_for_wiki_failure_rate(
    flywheel: CognitiveDecisionFlywheel, sample_skill: AutomationSkillRecord
) -> None:
    """失败率超过阈值时应生成 skill_to_cognitive_decision 洞察。"""
    flywheel.create_skill(sample_skill)
    for _ in range(7):
        flywheel.log_skill_usage("test-skill", status="failure")
    for _ in range(3):
        flywheel.log_skill_usage("test-skill", status="success")

    insights = flywheel.analyze_skill_for_wiki("test-skill")
    failure_insights = [i for i in insights if "失败" in i.target]
    assert len(failure_insights) >= 1
    assert failure_insights[0].direction == "skill_to_cognitive_decision"


def test_analyze_skill_for_wiki_exceptions(
    flywheel: CognitiveDecisionFlywheel, sample_skill: AutomationSkillRecord
) -> None:
    """同类异常多次出现时应生成洞察。"""
    flywheel.create_skill(sample_skill)
    for _ in range(2):
        flywheel.log_skill_usage("test-skill", status="failure", exception_type="ValueError")

    insights = flywheel.analyze_skill_for_wiki("test-skill")
    exc_insights = [i for i in insights if "异常" in i.target]
    assert len(exc_insights) >= 1


def test_analyze_skill_for_wiki_new_scenarios(
    flywheel: CognitiveDecisionFlywheel, sample_skill: AutomationSkillRecord
) -> None:
    """新场景多次出现时应生成洞察。"""
    flywheel.create_skill(sample_skill)
    for _ in range(3):
        flywheel.log_skill_usage(
            "test-skill", status="success", new_scenario=True, input_data="new_input"
        )

    insights = flywheel.analyze_skill_for_wiki("test-skill")
    ns_insights = [i for i in insights if "新场景" in i.target]
    assert len(ns_insights) >= 1


def test_analyze_skill_for_wiki_user_marked(
    flywheel: CognitiveDecisionFlywheel, sample_skill: AutomationSkillRecord
) -> None:
    """用户标记的记录应生成洞察。"""
    flywheel.create_skill(sample_skill)
    flywheel.log_skill_usage("test-skill", status="success", user_marked=True)

    insights = flywheel.analyze_skill_for_wiki("test-skill")
    um_insights = [i for i in insights if "用户标记" in i.target]
    assert len(um_insights) == 1
    assert um_insights[0].confidence == 0.9


def test_analyze_skill_for_wiki_missing_skill(flywheel: CognitiveDecisionFlywheel) -> None:
    """Skill 不存在时应返回空列表。"""
    assert flywheel.analyze_skill_for_wiki("nonexistent") == []


# ============================================================
# CognitiveDecisionFlywheel — Wiki 使用追踪
# ============================================================


def test_log_wiki_usage_and_get_usage(flywheel: CognitiveDecisionFlywheel) -> None:
    """log_wiki_usage 和 _get_wiki_usage 应正确记录和查询。"""
    flywheel.log_wiki_usage("page1.md", "read", "ctx")
    flywheel.log_wiki_usage("page1.md", "quote")
    assert flywheel._get_wiki_usage("page1.md", days=30) == 2
    assert flywheel._get_wiki_usage("page2.md", days=30) == 0


# ============================================================
# CognitiveDecisionFlywheel — Skill CRUD
# ============================================================


def test_create_and_get_skill(flywheel: CognitiveDecisionFlywheel, sample_skill: AutomationSkillRecord) -> None:
    """create_skill 和 get_skill 应能正确读写。"""
    assert flywheel.create_skill(sample_skill) is True
    retrieved = flywheel.get_skill("test-skill")
    assert retrieved is not None
    assert retrieved.skill_name == "test-skill"
    assert retrieved.description == "A test skill"
    assert retrieved.trigger_conditions == ["on demand"]
    assert retrieved.status == "active"
    assert retrieved.version == 1


def test_get_skill_missing_returns_none(flywheel: CognitiveDecisionFlywheel) -> None:
    """获取不存在的 Skill 应返回 None。"""
    assert flywheel.get_skill("missing") is None


def test_list_skills(flywheel: CognitiveDecisionFlywheel) -> None:
    """list_skills 应返回所有 Skill。"""
    s1 = AutomationSkillRecord(skill_name="s1", status="active")
    s2 = AutomationSkillRecord(skill_name="s2", status="proposed")
    flywheel.create_skill(s1)
    flywheel.create_skill(s2)

    all_skills = flywheel.list_skills()
    assert len(all_skills) == 2

    active_only = flywheel.list_skills(status="active")
    assert len(active_only) == 1
    assert active_only[0].skill_name == "s1"


def test_create_skill_updates_version_table(
    flywheel: CognitiveDecisionFlywheel, sample_skill: AutomationSkillRecord
) -> None:
    """create_skill 应同时向 skill_versions 表写入记录。"""
    flywheel.create_skill(sample_skill)
    with flywheel._conn() as conn:
        row = conn.execute(
            "SELECT * FROM skill_versions WHERE skill_name=?", ("test-skill",)
        ).fetchone()
    assert row is not None
    assert row["version"] == 1
    assert "initial create" in row["change_summary"]


# ============================================================
# CognitiveDecisionFlywheel — 飞轮周期
# ============================================================


def test_run_cycle_returns_expected_keys(
    flywheel: CognitiveDecisionFlywheel,
) -> None:
    """run_cycle 应返回包含预期键的结果字典。"""
    results = flywheel.run_cycle()
    assert "wiki_to_cognitive_decision" in results
    assert "behavior_to_cognitive_decision" in results
    assert "skill_to_cognitive_decision" in results
    assert "persona_driven" in results
    assert "cleanup" in results


def test_run_cycle_with_no_data(
    flywheel: CognitiveDecisionFlywheel,
) -> None:
    """无数据时 run_cycle 应返回空列表/字典。"""
    results = flywheel.run_cycle()
    assert results["wiki_to_cognitive_decision"] == []
    assert results["behavior_to_cognitive_decision"] == []
    assert results["skill_to_cognitive_decision"] == []


def test_generate_cycle_report_structure(
    flywheel: CognitiveDecisionFlywheel,
) -> None:
    """generate_cycle_report 应生成包含各章节标题的报告。"""
    results = flywheel.run_cycle()
    report = flywheel.generate_cycle_report(results)
    assert "认知决策飞轮周期报告" in report
    assert "Wiki → 认知决策资产" in report
    assert "Skill → 认知决策资产" in report


def test_generate_cycle_report_with_insights(
    flywheel: CognitiveDecisionFlywheel,
    tmp_wiki_dir: Path,
) -> None:
    """报告在有洞察时应包含具体信息。"""
    page = tmp_wiki_dir / "method.md"
    page.write_text(
        "---\n类型: 方法论\n置信度: 0.9\n触发场景:\n  - s\n---\n\n# 流程步骤\n\n1. a\n",
        encoding="utf-8",
    )
    for _ in range(10):
        flywheel.log_wiki_usage(str(page), "read")

    results = flywheel.run_cycle()
    report = flywheel.generate_cycle_report(results)
    assert "Wiki → 认知决策资产" in report


# ============================================================
# CognitiveDecisionFlywheel — 行为驱动任务记录
# ============================================================


def test_record_task_completed(flywheel: CognitiveDecisionFlywheel) -> None:
    """record_task_completed 应插入 task_history 记录。"""
    flywheel.record_task_completed("coding", "review", wiki_pages=["wiki/a.md"], input_summary="in")
    with flywheel._conn() as conn:
        row = conn.execute("SELECT * FROM task_history WHERE task_type=?", ("coding",)).fetchone()
    assert row is not None
    assert row["subtype"] == "review"
    assert json.loads(row["wiki_pages"]) == ["wiki/a.md"]


# ============================================================
# CognitiveDecisionFlywheel — 清理与归档
# ============================================================


def test_cleanup_stale_skills_marks_stale(flywheel: CognitiveDecisionFlywheel, fixed_now: datetime) -> None:
    """长时间未使用的 active skill 应被标记为 stale。"""
    old_time = (fixed_now - timedelta(days=90)).isoformat()[:19]
    # 直接插入数据库，绕过 create_skill 中的 datetime.now()
    with flywheel._conn() as conn:
        conn.execute(
            """INSERT INTO skills
               (skill_name, description, status, created_at, updated_at, generation_source)
               VALUES (?, ?, ?, ?, ?, ?)""",
            ("old-skill", "desc", "active", old_time, old_time, "manual"),
        )
        conn.commit()

    with patch("core.kia.ixion.datetime") as mock_dt:
        mock_dt.now.return_value = fixed_now
        mock_dt.fromisoformat = datetime.fromisoformat
        mock_dt.timedelta = timedelta
        archived = flywheel.cleanup_stale_skills(cleanup_days=60)

    assert "old-skill" not in archived  # behavior source 才会归档
    updated = flywheel.get_skill("old-skill")
    assert updated.status == "stale"


def test_cleanup_stale_skills_archives_behavior_source(
    flywheel: CognitiveDecisionFlywheel,
    fixed_now: datetime,
) -> None:
    """behavior 来源的 skill 应被归档并标记为 deprecated。"""
    old_time = (fixed_now - timedelta(days=90)).isoformat()[:19]
    with flywheel._conn() as conn:
        conn.execute(
            """INSERT INTO skills
               (skill_name, description, status, created_at, updated_at, generation_source)
               VALUES (?, ?, ?, ?, ?, ?)""",
            ("beh-skill", "desc", "active", old_time, old_time, "behavior"),
        )
        conn.commit()

    with patch("core.kia.ixion.datetime") as mock_dt:
        mock_dt.now.return_value = fixed_now
        mock_dt.fromisoformat = datetime.fromisoformat
        mock_dt.timedelta = timedelta
        archived = flywheel.cleanup_stale_skills(cleanup_days=60)

    assert "beh-skill" in archived
    updated = flywheel.get_skill("beh-skill")
    assert updated.status == "deprecated"
    archive_file = flywheel.wiki_base / "03-Archive" / "Skills" / "beh-skill-归档.md"
    assert archive_file.exists()


def test_cleanup_stale_skills_grace_period(
    flywheel: CognitiveDecisionFlywheel, fixed_now: datetime
) -> None:
    """在 grace period 内的 skill 不应被处理（通过 updated_at 判断）。"""
    recent = (fixed_now - timedelta(days=5)).isoformat()[:19]
    with flywheel._conn() as conn:
        conn.execute(
            """INSERT INTO skills
               (skill_name, description, status, created_at, updated_at, generation_source)
               VALUES (?, ?, ?, ?, ?, ?)""",
            ("recent-skill", "desc", "active", recent, recent, "manual"),
        )
        conn.commit()

    with patch("core.kia.ixion.datetime") as mock_dt:
        mock_dt.now.return_value = fixed_now
        mock_dt.fromisoformat = datetime.fromisoformat
        mock_dt.timedelta = timedelta
        archived = flywheel.cleanup_stale_skills(cleanup_days=60)

    assert "recent-skill" not in archived


def test_cleanup_stale_skills_archives_stale_after_grace_period(
    flywheel: CognitiveDecisionFlywheel,
    fixed_now: datetime,
) -> None:
    """已标记 stale 的 skill 超过 grace period 后应归档。"""
    stale_time = (fixed_now - timedelta(days=8)).isoformat()[:19]
    with flywheel._conn() as conn:
        conn.execute(
            """INSERT INTO skills
               (skill_name, description, status, created_at, updated_at, generation_source)
               VALUES (?, ?, ?, ?, ?, ?)""",
            ("stale-skill", "desc", "stale", stale_time, stale_time, "manual"),
        )
        conn.commit()

    with patch("core.kia.ixion.datetime") as mock_dt:
        mock_dt.now.return_value = fixed_now
        mock_dt.fromisoformat = datetime.fromisoformat
        mock_dt.timedelta = timedelta
        archived = flywheel.cleanup_stale_skills(cleanup_days=60, grace_period_days=7)

    assert archived == ["stale-skill"]
    updated = flywheel.get_skill("stale-skill")
    assert updated.status == "deprecated"
    archive_file = flywheel.wiki_base / "03-Archive" / "Skills" / "stale-skill-归档.md"
    assert archive_file.exists()


# ============================================================
# CognitiveDecisionFlywheel — 画像驱动便捷查询
# ============================================================


# ============================================================
# CognitiveDecisionFlywheel — fallback 与 persona 更新
# ============================================================


def test_fallback_from_metis_when_metis_unavailable(flywheel: CognitiveDecisionFlywheel) -> None:
    """Metis 不可用时 fallback 应返回默认参数。"""
    with patch.dict("sys.modules", {"core.kia.metis": None}):
        result = flywheel._fallback_from_metis()
    assert result["fallback"] == "metis_unavailable"
    assert "flywheel_params" in result
    assert result["flywheel_params"]["cycle_days"] == 5


def test_update_persona_when_persona_unavailable(
    tmp_wiki_dir: Path, tmp_db_path: Path, monkeypatch
) -> None:
    """PERSONA_AVAILABLE=False 时 update_persona 不应创建引擎。"""
    import core.config as _config_mod

    fake_cfg = MagicMock()
    fake_cfg.wiki_dir = tmp_wiki_dir
    monkeypatch.setattr(_config_mod, "get_config", lambda: fake_cfg)

    with patch("core.kia.ixion.PERSONA_AVAILABLE", False):
        fw = CognitiveDecisionFlywheel(wiki_base=str(tmp_wiki_dir), db_path=str(tmp_db_path))
        fw.update_persona(None)  # 不应抛出异常
        assert fw.persona_engine is None


# ============================================================
# 便捷函数
# ============================================================


def test_get_skill_gaps_no_persona() -> None:
    """get_skill_gaps 在无 persona 时应返回空列表。"""
    assert get_skill_gaps(None) == []


def test_get_personalized_skill_paths_no_persona() -> None:
    """get_personalized_skill_paths 在无 persona 时应返回空列表。"""
    assert get_personalized_skill_paths(None) == []


def test_get_verification_tasks_no_persona() -> None:
    """get_verification_tasks 在无 persona 时应返回空列表。"""
    assert get_verification_tasks(None, None, ["s1"]) == []


def test_run_flywheel_with_wiki_base(
    tmp_wiki_dir: Path,
    monkeypatch,
) -> None:
    """run_flywheel 便捷函数应能正常运行。"""
    import core.config as _config_mod

    fake_cfg = MagicMock()
    fake_cfg.wiki_dir = tmp_wiki_dir
    monkeypatch.setattr(_config_mod, "get_config", lambda: fake_cfg)

    results = run_flywheel(wiki_base=str(tmp_wiki_dir))
    assert "wiki_to_cognitive_decision" in results
    assert "skill_to_cognitive_decision" in results


def test_run_flywheel_report_mode_uses_persona_helper(
    tmp_wiki_dir: Path, monkeypatch
) -> None:
    """run_flywheel(report=True) 应复用画像驱动报告 helper。"""
    import core.kia.ixion as ixion

    persona = MagicMock()
    blindspot = MagicMock()
    calls = {}

    def fake_persona_flywheel(persona=None, blindspot=None, wiki_base=None):
        calls["persona"] = persona
        calls["blindspot"] = blindspot
        calls["wiki_base"] = wiki_base
        return "# persona report"

    monkeypatch.setattr(ixion, "run_persona_driven_flywheel", fake_persona_flywheel)

    rendered = ixion.run_flywheel(
        wiki_base=str(tmp_wiki_dir),
        persona=persona,
        blindspot=blindspot,
        report=True,
    )

    assert rendered == "# persona report"
    assert calls == {
        "persona": persona,
        "blindspot": blindspot,
        "wiki_base": str(tmp_wiki_dir),
    }


# ============================================================
# PersonaDrivenSkillEngine — 有 persona 时的行为 (mock)
# ============================================================


def test_persona_engine_analyze_skill_gaps_with_mock_persona() -> None:
    """使用 mock persona 时 analyze_skill_gaps 应返回缺口列表。"""
    persona = MagicMock()
    persona.cognitive.abstraction = 0.2
    persona.cognitive.system_view = 0.8
    persona.cognitive.skepticism = 0.8
    persona.cognitive.creativity = 0.8
    persona.cognitive.deduction = 0.8
    persona.cognitive.confidence = 0.9

    with patch("core.kia.ixion.PERSONA_AVAILABLE", True):
        engine = PersonaDrivenSkillEngine(persona=persona)
        gaps = engine.analyze_skill_gaps()

    assert len(gaps) >= 1
    assert gaps[0].dimension == "抽象↔具象"
    assert gaps[0].gap_severity == "critical"


def test_persona_engine_generate_skill_paths_with_mock_persona() -> None:
    """使用 mock persona 时 generate_skill_paths 应返回路径列表。"""
    persona = MagicMock()
    persona.cognitive.abstraction = 0.2
    persona.cognitive.system_view = 0.8
    persona.cognitive.skepticism = 0.8
    persona.cognitive.creativity = 0.8
    persona.cognitive.deduction = 0.7  # deductive style
    persona.cognitive.confidence = 0.9

    with patch("core.kia.ixion.PERSONA_AVAILABLE", True):
        engine = PersonaDrivenSkillEngine(persona=persona)
        gaps = engine.analyze_skill_gaps()
        paths = engine.generate_skill_paths(gaps)

    assert len(paths) >= 1
    assert paths[0].cognitive_style == "deductive"
    assert len(paths[0].stages) >= 3


def test_persona_engine_generate_tasks_by_values_with_mock_persona() -> None:
    """使用 mock persona 时 generate_tasks_by_values 应返回任务列表。"""
    persona = MagicMock()
    persona.cognitive.confidence = 0.9
    persona.value.correctness_vs_efficiency = 0.8
    persona.value.depth_vs_breadth = 0.8
    persona.value.perfection_vs_completion = 0.8
    persona.value.innovation_vs_safety = 0.8
    persona.value.autonomy_vs_collaboration = 0.8

    with patch("core.kia.ixion.PERSONA_AVAILABLE", True):
        engine = PersonaDrivenSkillEngine(persona=persona)
        tasks = engine.generate_tasks_by_values(["Python", "Go"])

    assert len(tasks) >= 1
    assert tasks[0]["skill"] == "Python"
    assert "validation_step" in tasks[0]


def test_persona_engine_get_flywheel_params_burst() -> None:
    """endurance_mode < 0.4 时应返回 burst 参数。"""
    persona = MagicMock()
    persona.energy.endurance_mode = 0.2
    persona.energy.startup_difficulty = 0.5
    persona.energy.switching_flexibility = 0.5

    with patch("core.kia.ixion.PERSONA_AVAILABLE", True):
        engine = PersonaDrivenSkillEngine(persona=persona)
        params = engine.get_flywheel_params()

    assert params["cycle_days"] == 3
    assert params["intensity"] == "high"


def test_persona_engine_get_flywheel_params_steady() -> None:
    """endurance_mode > 0.6 时应返回 steady 参数。"""
    persona = MagicMock()
    persona.energy.endurance_mode = 0.8
    persona.energy.startup_difficulty = 0.5
    persona.energy.switching_flexibility = 0.5

    with patch("core.kia.ixion.PERSONA_AVAILABLE", True):
        engine = PersonaDrivenSkillEngine(persona=persona)
        params = engine.get_flywheel_params()

    assert params["cycle_days"] == 7
    assert params["intensity"] == "medium"


def test_persona_engine_generate_verification_tasks_with_mock_blindspot() -> None:
    """使用 mock blindspot 时应返回验证任务。"""
    persona = MagicMock()
    blindspot = MagicMock()
    bs = MagicMock()
    bs.type = "framing"
    blindspot.confirmed = [bs]
    blindspot.suspected = []

    with patch("core.kia.ixion.PERSONA_AVAILABLE", True):
        engine = PersonaDrivenSkillEngine(persona=persona, blindspot=blindspot)
        tasks = engine.generate_verification_tasks(["skill_a"])

    assert len(tasks) >= 1
    assert tasks[0].related_skill == "skill_a"
    assert tasks[0].related_blindspot_type == "framing"


def test_persona_engine_format_insights_with_data() -> None:
    """format_persona_insights 在有数据时应生成完整报告。"""
    gap = PersonaSkillGap(
        dimension="抽象↔具象",
        current_score=0.2,
        target_score=0.5,
        gap_severity="critical",
        recommended_skill_category="模式识别",
        rationale="r",
    )
    path = SkillPath(
        path_id="p1",
        title="提升抽象能力",
        description="d",
        stages=[{"name": "s1", "type": "t1", "description": "d1"}],
        cognitive_style="deductive",
        estimated_duration="2-4周",
        priority="high",
    )
    task = {
        "skill": "Python",
        "base_task": "bt",
        "scope": "s",
        "deliverable": "d",
        "method_constraint": "m",
        "validation_step": "v",
    }
    vtask = SkillVerificationTask(
        task_id="v1",
        task_type="framing_challenge",
        description="d",
        related_skill="Python",
        related_blindspot_type="framing",
        verification_method="m",
        expected_outcome="o",
    )

    engine = PersonaDrivenSkillEngine(persona=None)
    text = engine.format_persona_insights([gap], [path], [task], [vtask])
    assert "能力短板识别" in text
    assert "抽象↔具象" in text
    assert "个性化学习路径" in text
    assert "提升抽象能力" in text
    assert "任务生成策略" in text
    assert "盲区验证任务" in text


def test_persona_engine_build_path_abstract_high() -> None:
    """abstraction > 0.6 时应在路径前插入概念框架阶段。"""
    persona = MagicMock()
    persona.cognitive.deduction = 0.5  # balanced
    persona.cognitive.abstraction = 0.8

    gap = PersonaSkillGap(
        dimension="抽象↔具象",
        current_score=0.2,
        target_score=0.5,
        gap_severity="high",
        recommended_skill_category="模式识别",
        rationale="r",
    )

    with patch("core.kia.ixion.PERSONA_AVAILABLE", True):
        engine = PersonaDrivenSkillEngine(persona=persona)
        path = engine._build_path_for_gap(gap, persona.cognitive)

    assert path is not None
    stage_names = [s["name"] for s in path.stages]
    assert "概念框架" in stage_names


def test_persona_engine_build_path_abstract_low() -> None:
    """abstraction < 0.4 且 style != inductive 时应在描述中强调动手。"""
    # 使用 deductive style (deduction > 0.6) 配合 abstraction < 0.4
    # deductive stages contain '理解' which gets replaced to '通过动手理解'
    persona = MagicMock()
    persona.cognitive.deduction = 0.7  # deductive style
    persona.cognitive.abstraction = 0.2

    gap = PersonaSkillGap(
        dimension="抽象↔具象",
        current_score=0.2,
        target_score=0.5,
        gap_severity="high",
        recommended_skill_category="模式识别",
        rationale="r",
    )

    with patch("core.kia.ixion.PERSONA_AVAILABLE", True):
        engine = PersonaDrivenSkillEngine(persona=persona)
        path = engine._build_path_for_gap(gap, persona.cognitive)

    assert path is not None
    descriptions = [s["description"] for s in path.stages]
    assert any("动手" in d for d in descriptions)


# ============================================================
# CognitiveDecisionFlywheel — _save_skill_paths / _save_verification_tasks
# ============================================================


def test_save_skill_paths(flywheel: CognitiveDecisionFlywheel) -> None:
    """_save_skill_paths 应正确写入数据库。"""
    path = SkillPath(
        path_id="p_test",
        title="Test Path",
        description="d",
        stages=[{"name": "s1", "type": "t1"}],
        cognitive_style="balanced",
        estimated_duration="1周",
        priority="medium",
    )
    flywheel._save_skill_paths([path])

    with flywheel._conn() as conn:
        rows = conn.execute("SELECT * FROM skill_paths WHERE status='active'").fetchall()
    assert len(rows) == 1
    assert rows[0]["path_id"] == "p_test"


def test_save_verification_tasks_avoids_duplicates(flywheel: CognitiveDecisionFlywheel) -> None:
    """_save_verification_tasks 应避免重复创建 pending 任务。"""
    task = SkillVerificationTask(
        task_id="vt1",
        task_type="framing_challenge",
        description="d",
        related_skill="s1",
        related_blindspot_type="framing",
        verification_method="m",
        expected_outcome="o",
    )
    flywheel._save_verification_tasks([task])
    flywheel._save_verification_tasks([task])  # 再次保存

    with flywheel._conn() as conn:
        rows = conn.execute(
            "SELECT * FROM skill_verification_tasks WHERE status='pending'"
        ).fetchall()
    assert len(rows) == 1


# ============================================================
# CognitiveDecisionFlywheel — _log_persona_cycle
# ============================================================


def test_log_persona_cycle(flywheel: CognitiveDecisionFlywheel) -> None:
    """_log_persona_cycle 应正确写入日志表。"""
    flywheel._log_persona_cycle(
        {
            "gaps": [1, 2],
            "paths": [1],
            "verifications": [],
            "flywheel_params": {"cycle_days": 5},
        }
    )
    with flywheel._conn() as conn:
        row = conn.execute(
            "SELECT * FROM persona_flywheel_logs ORDER BY log_id DESC LIMIT 1"
        ).fetchone()
    assert row is not None
    assert row["gaps_detected"] == 2
    assert row["paths_created"] == 1
    assert row["verifications_created"] == 0
    assert "cycle_days" in row["flywheel_params"]


# ============================================================
# CognitiveDecisionFlywheel — handle_event skill_executed / skill_deviated
# ============================================================


def test_handle_event_skill_executed_calls_log_method(
    flywheel: CognitiveDecisionFlywheel, sample_skill: AutomationSkillRecord
) -> None:
    """handle_event skill_executed 应调用 _log_skill_execution 并记录成功状态。"""
    flywheel.create_skill(sample_skill)
    with patch.object(flywheel, "log_skill_usage") as mock_log:
        flywheel.handle_event(
            "skill_executed",
            {
                "skill_name": "test-skill",
                "input_data": "in",
                "output_data": "out",
                "status": "success",
            },
        )
    mock_log.assert_called_once_with(
        skill_name="test-skill",
        input_data="in",
        output_data="out",
        status="success",
        exception_type="",
        exception_detail="",
        new_scenario=False,
        user_marked=False,
    )


# ============================================================
# CognitiveDecisionFlywheel — _run_persona_driven_cycle (mock)
# ============================================================


def test_run_persona_driven_cycle_with_mock_engine(flywheel: CognitiveDecisionFlywheel) -> None:
    """_run_persona_driven_cycle 应返回包含各键的字典。"""
    engine = MagicMock()
    engine.analyze_skill_gaps.return_value = []
    engine.generate_skill_paths.return_value = []
    engine.generate_tasks_by_values.return_value = []
    engine.get_flywheel_params.return_value = {"cycle_days": 5}
    engine.generate_verification_tasks.return_value = []
    flywheel.persona_engine = engine

    result = flywheel._run_persona_driven_cycle()
    assert "gaps" in result
    assert "paths" in result
    assert "tasks" in result
    assert "flywheel_params" in result
    assert "verifications" in result


# ============================================================
# P2-13: 自动执行测试
# ============================================================


def test_execute_insights_wiki_to_cognitive_decision_auto_applicable(
    tmp_wiki_dir: Path,
    tmp_db_path: Path,
) -> None:
    """auto_applicable=True 的 wiki insight 应创建认知决策资产。"""
    flywheel = CognitiveDecisionFlywheel(wiki_base=str(tmp_wiki_dir), db_path=str(tmp_db_path))

    # 创建一个测试 wiki 页面
    page = tmp_wiki_dir / "test_page.md"
    page.write_text("---\ntype: 方法论\n---\n\n# 测试方法论\n\n步骤1. 做某事\n", encoding="utf-8")

    insight = FlywheelInsight(
        direction="wiki_to_cognitive_decision",
        source=str(page),
        target="测试方法论认知决策资产",
        confidence=0.8,
        reason="类型为方法论",
        auto_applicable=True,
    )
    results = {
        "wiki_to_cognitive_decision": [insight],
        "behavior_to_cognitive_decision": [],
        "skill_to_cognitive_decision": [],
    }

    executed = flywheel.execute_insights(results)
    assert executed["count"] == 1
    assert any("标记为认知决策资产" in a for a in executed["actions"])

    # 验证 frontmatter 被更新
    content = page.read_text(encoding="utf-8")
    assert "cognitive_decision_asset_candidate" in content
    assert "cognitive_decision_asset_id" in content

    assets = flywheel.list_cognitive_decision_assets()
    assert len(assets) == 1
    assert assets[0].title == "测试方法论认知决策资产"
    assert flywheel.get_skill("测试方法论认知决策资产") is None


def test_execute_insights_enforce_submits_proposal_without_touching_page(
    tmp_wiki_dir: Path,
    tmp_db_path: Path,
    monkeypatch,
) -> None:
    db = tmp_wiki_dir / ".mnemos" / "trusted.db"
    fake_config = SimpleNamespace(
        wiki_dir=tmp_wiki_dir,
        database_dir=tmp_wiki_dir / ".mnemos",
        get=lambda key, default=None: {
            "trusted_push.mode": "enforce",
            "trusted_push.db_path": str(db),
        }.get(key, default),
    )
    monkeypatch.setattr("core.trust.config.get_config", lambda: fake_config)
    flywheel = CognitiveDecisionFlywheel(wiki_base=str(tmp_wiki_dir), db_path=str(tmp_db_path))
    page = tmp_wiki_dir / "test_page.md"
    page.write_text("---\ntype: 方法论\n---\n\n# 测试方法论\n", encoding="utf-8")
    original = page.read_text(encoding="utf-8")
    insight = FlywheelInsight(
        direction="wiki_to_cognitive_decision",
        source=str(page),
        target="测试方法论认知决策资产",
        confidence=0.8,
        reason="类型为方法论",
        auto_applicable=True,
    )

    executed = flywheel.execute_insights(
        {
            "wiki_to_cognitive_decision": [insight],
            "behavior_to_cognitive_decision": [],
            "skill_to_cognitive_decision": [],
        }
    )

    assert executed["count"] == 1
    assert page.read_text(encoding="utf-8") == original
    proposals = ProposalQueue(db, wiki_base=tmp_wiki_dir).list()
    assert proposals[0].candidate.source == "ixion_flywheel"


def test_execute_insights_behavior_to_cognitive_decision_auto_applicable(
    tmp_wiki_dir: Path, tmp_db_path: Path
) -> None:
    """auto_applicable=True 的 behavior insight 应创建认知决策资产。"""
    flywheel = CognitiveDecisionFlywheel(wiki_base=str(tmp_wiki_dir), db_path=str(tmp_db_path))

    insight = FlywheelInsight(
        direction="behavior_to_cognitive_decision",
        source="coding/refactor",
        target="coding/refactor 认知决策资产",
        confidence=0.75,
        reason="近30天重复完成5次同类任务",
        auto_applicable=True,
        asset_type="verification_recipe",
    )
    results = {
        "wiki_to_cognitive_decision": [],
        "behavior_to_cognitive_decision": [insight],
        "skill_to_cognitive_decision": [],
    }

    executed = flywheel.execute_insights(results)
    assert executed["count"] == 1
    assert any("从行为模式创建认知决策资产" in a for a in executed["actions"])

    assets = flywheel.list_cognitive_decision_assets()
    assert len(assets) == 1
    assert assets[0].asset_type == "verification_recipe"
    assert flywheel.get_skill("coding/refactor 认知决策资产") is None


def test_execute_insights_skill_to_cognitive_decision_marks_review(
    tmp_wiki_dir: Path,
    tmp_db_path: Path,
) -> None:
    """skill_to_cognitive_decision insight 应在来源 wiki 页面标记 needs_review。"""
    flywheel = CognitiveDecisionFlywheel(wiki_base=str(tmp_wiki_dir), db_path=str(tmp_db_path))

    # 创建来源 wiki 页面
    page = tmp_wiki_dir / "source_page.md"
    page.write_text("---\ntype: 经验法则\n---\n\n# 源页面\n", encoding="utf-8")

    # 先创建 skill 并关联到页面
    skill = AutomationSkillRecord(
        skill_name="测试技能",
        description="测试",
        source_wiki_pages=[str(page)],
        status="active",
    )
    flywheel.create_skill(skill)

    insight = FlywheelInsight(
        direction="skill_to_cognitive_decision",
        source="测试技能",
        target="测试技能 失败处理指南",
        confidence=0.7,
        reason="失败率 40%，超过阈值",
    )
    results = {
        "wiki_to_cognitive_decision": [],
        "behavior_to_cognitive_decision": [],
        "skill_to_cognitive_decision": [insight],
    }

    executed = flywheel.execute_insights(results)
    assert executed["count"] == 1

    # 验证 frontmatter 被标记
    content = page.read_text(encoding="utf-8")
    assert "needs_review" in content
    assert "review_reason" in content


def test_execute_insights_skips_non_auto_applicable(tmp_wiki_dir: Path, tmp_db_path: Path) -> None:
    """auto_applicable=False 的 insight 不应被执行。"""
    flywheel = CognitiveDecisionFlywheel(wiki_base=str(tmp_wiki_dir), db_path=str(tmp_db_path))

    page = tmp_wiki_dir / "test.md"
    page.write_text("---\n---\n\n# 测试\n", encoding="utf-8")

    insight = FlywheelInsight(
        direction="wiki_to_cognitive_decision",
        source=str(page),
        target="测试助手",
        confidence=0.8,
        reason="测试",
        auto_applicable=False,
    )
    results = {
        "wiki_to_cognitive_decision": [insight],
        "behavior_to_cognitive_decision": [],
        "skill_to_cognitive_decision": [],
    }

    executed = flywheel.execute_insights(results)
    assert executed["count"] == 0


def test_write_report_creates_files(
    tmp_wiki_dir: Path,
    tmp_db_path: Path,
) -> None:
    """write_report 应在 wiki/06-Retrospectives/flywheel/ 下创建报告。"""
    flywheel = CognitiveDecisionFlywheel(wiki_base=str(tmp_wiki_dir), db_path=str(tmp_db_path))

    results = {
        "wiki_to_cognitive_decision": [],
        "behavior_to_cognitive_decision": [],
        "skill_to_cognitive_decision": [],
        "persona_driven": {},
        "executed": {"actions": ["[mnemos-auto] flywheel: 测试操作"], "errors": [], "count": 1},
    }

    report_path = flywheel.write_report(results)
    assert report_path is not None
    assert report_path.exists()
    assert "flywheel_report_" in report_path.name

    # 验证报告内容
    content = report_path.read_text(encoding="utf-8")
    assert "自动执行日志" in content
    assert "测试操作" in content

    # 验证执行摘要也被创建
    summary = flywheel._last_report_write_result.summary_path
    assert summary is not None
    assert summary.exists()
    summary_content = summary.read_text(encoding="utf-8")
    assert len(summary_content) >= 200
    assert "验证与追踪" in summary_content
    from scripts.wiki_lint import extract_frontmatter, fm_get

    for generated in (report_path, summary):
        frontmatter, _body = extract_frontmatter(
            generated.read_text(encoding="utf-8")
        )
        assert frontmatter is not None
        assert fm_get(frontmatter, "status") == "active"
        assert fm_get(frontmatter, "knowledge_stage") == "P2"
        assert fm_get(frontmatter, "source_count") == 1
        assert fm_get(frontmatter, "evidence_level") == "single"


def test_run_cycle_does_not_claim_intercepted_report(
    tmp_wiki_dir: Path, tmp_db_path: Path, monkeypatch
) -> None:
    """Enforce interception must not expose a path or trigger a report commit."""
    from core.trust.vault_mutation_service import TrustedVaultMutationResult

    flywheel = CognitiveDecisionFlywheel(wiki_base=str(tmp_wiki_dir), db_path=str(tmp_db_path))
    monkeypatch.setattr(flywheel, "list_skills", lambda: [])
    monkeypatch.setattr(flywheel, "cleanup_stale_skills", lambda: [])
    monkeypatch.setattr(flywheel, "execute_insights", lambda _results: {"actions": [], "errors": [], "count": 0})
    commits = []
    monkeypatch.setattr(flywheel, "_git_commit_changes", lambda *args: commits.append(args))
    monkeypatch.setattr(
        "core.kia.flywheel_report.submit_or_write_markdown_with_decision",
        lambda *args, **kwargs: TrustedVaultMutationResult(
            action="intercept", mode="enforce", proposal_id="proposal-1", status="pending"
        ),
    )

    results = flywheel.run_cycle()

    assert "report_path" not in results
    assert commits == []
    assert not list(tmp_wiki_dir.rglob("flywheel_report_*.md"))
    assert flywheel._last_report_write_result.report_receipt.intercepted is True


def test_run_cycle_includes_executed_and_report(
    tmp_wiki_dir: Path,
    tmp_db_path: Path,
) -> None:
    """run_cycle 应包含 executed 和 report_path 键。"""
    flywheel = CognitiveDecisionFlywheel(wiki_base=str(tmp_wiki_dir), db_path=str(tmp_db_path))

    results = flywheel.run_cycle()
    assert "executed" in results
    assert "report_path" in results
    # 报告路径应为字符串
    assert isinstance(results["report_path"], str)
