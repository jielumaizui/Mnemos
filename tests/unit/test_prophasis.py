"""
Prophasis (PreFlightInjector) 全面单元测试

覆盖项：
1. 数据类 — ChecklistItem、LoadedKnowledge
2. PreFlightInjector 公共方法 — inject、format_for_context、mark_checklist_used
3. PreFlightInjector 内部方法 — _merge_behavior_constraints、_load_full、_find_latest_version、
  _parse_retrospective、_parse_checklist_item、_extract_scenario_tags、_filter_by_scenario、
  _sort_by_relevance、_apply_echo_chamber_breaker、_calc_persona_bonus、_generate_checklist_from_page、
  _find_wiki_fallback、_get_checklist_for_type、_build_fallback_lessons_summary、_warm_checklist_cache
4. 外部依赖隔离 — get_config、PersonaStore、sqlite3 缓存
"""

import pytest
from datetime import datetime, timedelta
from unittest.mock import MagicMock

from core.kia.kairos import TimeWindow, TimeWindowType
from core.kia.prophasis import (
    ChecklistItem,
    LoadedKnowledge,
    PreFlightInjector,
    BEHAVIOR_CONSTRAINTS,
)

# ---------- Fixtures ----------


@pytest.fixture
def sample_checklist_items():
    """返回一组多样化的 ChecklistItem 用于测试。"""
    return [
        ChecklistItem(
            item="检查并发安全",
            source="retro-v1",
            severity="critical",
            freshness_score=0.9,
            hit_count=5,
            ignore_count=1,
            last_hit=(datetime.now() - timedelta(days=3)).isoformat(),
            applies_when=["target:vip"],
            trigger_keywords=["并发", "安全"],
        ),
        ChecklistItem(
            item="优化查询性能",
            source="retro-v1",
            severity="high",
            freshness_score=0.7,
            hit_count=3,
            ignore_count=0,
            last_hit=(datetime.now() - timedelta(days=10)).isoformat(),
            applies_when=["scale:large"],
            trigger_keywords=["性能", "优化"],
        ),
        ChecklistItem(
            item="添加单元测试",
            source="retro-v1",
            severity="medium",
            freshness_score=0.5,
            hit_count=1,
            ignore_count=2,
            applies_when=["target:general"],
            not_applies_when=["target:price_sensitive"],
            trigger_keywords=["测试"],
        ),
        ChecklistItem(
            item="更新文档",
            source="retro-v1",
            severity="low",
            freshness_score=0.3,
            hit_count=0,
            ignore_count=0,
            trigger_keywords=["文档"],
        ),
    ]


@pytest.fixture
def sample_loaded_knowledge(sample_checklist_items):
    """返回一个 LoadedKnowledge 实例。"""
    return LoadedKnowledge(
        task_type="coding",
        subtype="debug",
        version=1,
        checklist=sample_checklist_items[:2],
        lessons_summary="上次复盘要点",
        loaded_at=datetime.now().isoformat(),
        is_compact=False,
        total_items=4,
        hit_items=3,
        ignored_items=1,
    )


@pytest.fixture
def injector(tmp_path, monkeypatch):
    """返回使用临时 wiki 目录的 PreFlightInjector，并隔离外部依赖。"""
    # Patch get_config 返回 FakeConfig
    fake_config = MagicMock()
    fake_config.wiki_dir = tmp_path
    fake_config.database_dir = tmp_path / "db"
    monkeypatch.setattr("core.kia.prophasis.get_config", lambda: fake_config)

    # Patch PersonaStore 避免加载真实画像
    mock_persona_store_cls = MagicMock()
    mock_persona_store_cls.return_value.load_persona.return_value = (None, None)
    monkeypatch.setattr("core.kia.prophasis.PersonaStore", mock_persona_store_cls)

    # 使用临时 wiki_base 初始化，避免 _warm_checklist_cache 访问真实文件
    wiki_base = tmp_path
    (wiki_base / "04-Concepts").mkdir(parents=True, exist_ok=True)
    (wiki_base / "06-Retrospectives").mkdir(parents=True, exist_ok=True)

    injector = PreFlightInjector(wiki_base=str(wiki_base))
    injector.current_persona = None  # 确保无画像
    return injector


# ========== 数据类 ==========


def test_checklist_item_defaults():
    """ChecklistItem 默认值应正确。"""
    item = ChecklistItem(item="测试项", source="test")
    assert item.severity == "medium"
    assert item.freshness_score == 1.0
    assert item.hit_count == 0
    assert item.ignore_count == 0
    assert item.applies_when == []
    assert item.not_applies_when == []
    assert item.trigger_keywords == []
    assert item.risk_patterns == []
    assert item.detail == ""


def test_loaded_knowledge_defaults():
    """LoadedKnowledge 默认值应正确。"""
    lk = LoadedKnowledge(
        task_type="coding",
        subtype="debug",
        version=1,
        checklist=[],
        lessons_summary="",
        loaded_at="2024-01-01T00:00:00",
    )
    assert lk.is_compact is False
    assert lk.total_items == 0
    assert lk.hit_items == 0
    assert lk.ignored_items == 0


# ========== PreFlightInjector 初始化 ==========


def test_injector_init_with_wiki_base(tmp_path, monkeypatch):
    """传入 wiki_base 时应正确设置路径。"""
    fake_config = MagicMock()
    fake_config.wiki_dir = tmp_path
    fake_config.database_dir = tmp_path / "db"
    monkeypatch.setattr("core.kia.prophasis.get_config", lambda: fake_config)

    mock_persona_store_cls = MagicMock()
    mock_persona_store_cls.return_value.load_persona.return_value = (None, None)
    monkeypatch.setattr("core.kia.prophasis.PersonaStore", mock_persona_store_cls)

    (tmp_path / "04-Concepts").mkdir(parents=True, exist_ok=True)
    (tmp_path / "06-Retrospectives").mkdir(parents=True, exist_ok=True)

    injector = PreFlightInjector(wiki_base=str(tmp_path))
    assert injector.WIKI_BASE == tmp_path
    assert injector.RETROSPECTIVES_DIR == tmp_path / "06-Retrospectives"


# ========== inject() 公共方法 ==========


def test_inject_immediate_loads_full(injector):
    """IMMEDIATE 时间窗口应触发完整装载。"""
    tw = TimeWindow(window=TimeWindowType.IMMEDIATE, days_until=0)
    result = injector.inject("coding", "debug", tw, "")

    # 无专用复盘文件时返回 fallback（含行为约束）
    assert result is not None
    assert isinstance(result, LoadedKnowledge)
    assert result.task_type == "coding"
    assert result.subtype == "debug"


def test_inject_short_loads_full(injector):
    """SHORT 时间窗口应触发完整装载。"""
    tw = TimeWindow(window=TimeWindowType.SHORT, days_until=3)
    result = injector.inject("coding", "debug", tw, "")
    assert result is not None
    assert result.task_type == "coding"


def test_inject_medium_returns_none(injector):
    """MEDIUM 时间窗口应返回 None（不装载）。"""
    tw = TimeWindow(window=TimeWindowType.MEDIUM, days_until=15)
    result = injector.inject("coding", "debug", tw, "")
    assert result is None


def test_inject_long_returns_none(injector):
    """LONG 时间窗口应返回 None（不装载）。"""
    tw = TimeWindow(window=TimeWindowType.LONG, days_until=60)
    result = injector.inject("coding", "debug", tw, "")
    assert result is None


def test_inject_periodic_loads_full(injector):
    """PERIODIC 时间窗口应触发完整装载。"""
    tw = TimeWindow(
        window=TimeWindowType.PERIODIC,
        days_until=0,
        is_periodic=True,
        period="weekly",
    )
    result = injector.inject("review", "weekly", tw, "")
    assert result is not None
    assert result.task_type == "review"


def test_inject_with_context_text(injector):
    """传入 context_text 时应用于场景适配。"""
    tw = TimeWindow(window=TimeWindowType.IMMEDIATE, days_until=0)
    result = injector.inject("coding", "debug", tw, "这是一个 vip 客户的大数据分析项目")
    assert result is not None


def test_inject_does_not_load_unscoped_legacy_layer5_experiences(injector, tmp_path):
    """legacy Layer-5 rows cannot enter a preflight prompt without an ACL."""
    from core.reflection.reflection_store import ReflectionStore

    db_path = tmp_path / "db" / "reflections.db"
    store = ReflectionStore(str(db_path))
    store.add_experience(
        {
            "type": "insight_pattern",
            "dimension": "attention",
            "summary": "避免重复读取同一文件",
            "confidence": 0.85,
            "reason": "多次读取同一文件是分析瘫痪信号",
        }
    )

    tw = TimeWindow(window=TimeWindowType.IMMEDIATE, days_until=0)
    result = injector.inject("coding", "debug", tw, "")
    assert result is not None
    items_text = [item.item for item in result.checklist]
    assert not any("避免重复读取同一文件" in text for text in items_text)
    assert not any(item.source == "layer5_experience" for item in result.checklist)


# ========== _merge_behavior_constraints ==========


def test_merge_behavior_constraints_adds_defaults(sample_checklist_items):
    """合并时应将行为约束添加到列表前部。"""
    merged = PreFlightInjector._merge_behavior_constraints(sample_checklist_items)
    # 前4个是 BEHAVIOR_CONSTRAINTS
    assert len(merged) == len(sample_checklist_items) + len(BEHAVIOR_CONSTRAINTS)
    for i, bc in enumerate(BEHAVIOR_CONSTRAINTS):
        assert merged[i].item == bc.item


def test_merge_behavior_constraints_deduplicates():
    """重复项应被去重。"""
    duplicate = ChecklistItem(
        item=BEHAVIOR_CONSTRAINTS[0].item,
        source="duplicate",
    )
    merged = PreFlightInjector._merge_behavior_constraints([duplicate])
    # BEHAVIOR_CONSTRAINTS 已包含该项，不应重复
    items_text = [m.item for m in merged]
    assert items_text.count(BEHAVIOR_CONSTRAINTS[0].item) == 1


def test_merge_behavior_constraints_empty_input():
    """空输入应只返回行为约束。"""
    merged = PreFlightInjector._merge_behavior_constraints([])
    assert len(merged) == len(BEHAVIOR_CONSTRAINTS)


# ========== _load_full / fallback 路径 ==========


def test_load_full_no_retrospective_uses_fallback(injector):
    """无专用复盘文件时应使用 Wiki fallback。"""
    result = injector._load_full("coding", "debug", "")
    assert result is not None
    assert result.version == 1
    assert (
        "未命中专用复盘文件" in result.lessons_summary
        or "通用 AI 行为约束" in result.lessons_summary
    )


def test_load_full_with_retrospective_file(injector):
    """存在专用复盘文件时应正确解析。"""
    task_dir = injector.RETROSPECTIVES_DIR / "coding"
    task_dir.mkdir(parents=True, exist_ok=True)

    retro_file = task_dir / "debug-v1.md"
    retro_file.write_text(
        "---\n"
        "version: 3\n"
        "lessons_summary: 核心教训\n"
        "checklist:\n"
        "  - item: 检查边界条件\n"
        "    source: retro\n"
        "    severity: high\n"
        "    freshness_score: 0.8\n"
        "---\n"
        "# 复盘内容\n",
        encoding="utf-8",
    )

    result = injector._load_full("coding", "debug", "")
    assert result is not None
    assert result.version == 3
    assert result.lessons_summary == "核心教训"
    assert len(result.checklist) >= 1
    # 行为约束被合并
    assert any("分析瘫痪" in item.item for item in result.checklist)


def test_load_full_with_active_symlink(injector):
    """active 软链接应被正确解析。"""
    task_dir = injector.RETROSPECTIVES_DIR / "coding"
    task_dir.mkdir(parents=True, exist_ok=True)

    real_file = task_dir / "debug-v2.md"
    real_file.write_text(
        "---\nversion: 2\nlessons_summary: 从v2学习\nchecklist: []\n---\n",
        encoding="utf-8",
    )
    symlink = task_dir / "debug-active.md"
    symlink.symlink_to(real_file)

    result = injector._load_full("coding", "debug", "")
    assert result is not None
    assert result.version == 2


# ========== _find_latest_version ==========


def test_find_latest_version_no_dir(injector):
    """目录不存在时应返回 None。"""
    result = injector._find_latest_version("nonexistent", "subtype")
    assert result is None


def test_find_latest_version_highest_number(injector):
    """应返回版本号最高的文件。"""
    task_dir = injector.RETROSPECTIVES_DIR / "coding"
    task_dir.mkdir(parents=True, exist_ok=True)

    (task_dir / "debug-v1.md").write_text("---\nversion: 1\n---\n", encoding="utf-8")
    (task_dir / "debug-v3.md").write_text("---\nversion: 3\n---\n", encoding="utf-8")
    (task_dir / "debug-v2.md").write_text("---\nversion: 2\n---\n", encoding="utf-8")

    result = injector._find_latest_version("coding", "debug")
    assert result is not None
    assert result.name == "debug-v3.md"


# ========== _parse_retrospective ==========


def test_parse_retrospective_valid(injector, tmp_path):
    """正确解析 frontmatter 和 body。"""
    f = tmp_path / "test.md"
    f.write_text("---\nversion: 1\n---\n\n# Body\ncontent", encoding="utf-8")
    fm, body = injector._parse_retrospective(f)
    assert fm == {"version": 1}
    assert body == "# Body\ncontent"


def test_parse_retrospective_no_frontmatter(injector, tmp_path):
    """无 frontmatter 时应返回空 dict 和完整内容。"""
    f = tmp_path / "test.md"
    f.write_text("# No frontmatter\ncontent", encoding="utf-8")
    fm, body = injector._parse_retrospective(f)
    assert fm == {}
    assert body == "# No frontmatter\ncontent"


def test_parse_retrospective_invalid_yaml(injector, tmp_path):
    """无效 YAML 时应返回空 dict。"""
    f = tmp_path / "test.md"
    f.write_text("---\ninvalid: yaml: [\n---\n\nbody", encoding="utf-8")
    fm, body = injector._parse_retrospective(f)
    assert fm == {}


def test_parse_retrospective_io_error(injector, tmp_path):
    """IO 错误时应返回 (None, '')。"""
    f = tmp_path / "nonexistent.md"
    fm, body = injector._parse_retrospective(f)
    assert fm is None
    assert body == ""


# ========== _parse_checklist_item ==========


def test_parse_checklist_item_full(injector):
    """完整字段解析。"""
    raw = {
        "item": "测试项",
        "source": "test",
        "severity": "critical",
        "freshness_score": 0.8,
        "hit_count": 3,
        "ignore_count": 2,
        "ignore_reasons": ["误报", "低相关"],
        "last_hit": "2024-01-01T00:00:00",
        "last_ignore": "2024-01-02T00:00:00",
        "applies_when": ["target:vip"],
        "not_applies_when": ["target:price_sensitive"],
        "trigger_keywords": ["测试"],
        "risk_patterns": ["风险"],
        "detail": "详情",
    }
    item = injector._parse_checklist_item(raw)
    assert item.item == "测试项"
    assert item.source == "test"
    assert item.severity == "critical"
    assert item.freshness_score == 0.8
    assert item.hit_count == 3
    assert item.ignore_count == 2
    assert item.ignore_reasons == ["误报", "低相关"]
    assert item.last_hit == "2024-01-01T00:00:00"
    assert item.last_ignore == "2024-01-02T00:00:00"
    assert item.applies_when == ["target:vip"]
    assert item.not_applies_when == ["target:price_sensitive"]
    assert item.trigger_keywords == ["测试"]
    assert item.risk_patterns == ["风险"]
    assert item.detail == "详情"


def test_parse_checklist_item_defaults(injector):
    """缺失字段应使用默认值。"""
    raw = {"item": "最小项"}
    item = injector._parse_checklist_item(raw)
    assert item.severity == "medium"
    assert item.freshness_score == 1.0
    assert item.hit_count == 0
    assert item.ignore_count == 0
    assert item.ignore_reasons == []
    assert item.last_ignore is None
    assert item.source == ""


# ========== _extract_scenario_tags ==========


def test_extract_scenario_tags_price_sensitive(injector):
    """价格敏感关键词应提取对应标签。"""
    tags = injector._extract_scenario_tags("用户很在意价格，想要低价优惠")
    assert "target:price_sensitive" in tags


def test_extract_scenario_tags_vip(injector):
    """VIP 关键词应提取对应标签。"""
    tags = injector._extract_scenario_tags("这是VIP客户的高端项目")
    assert "target:vip" in tags


def test_extract_scenario_tags_scale(injector):
    """规模关键词应提取对应标签。"""
    tags = injector._extract_scenario_tags("这是一个大规模全网推广，千人参与")
    assert "scale:large" in tags


def test_extract_scenario_tags_multiple(injector):
    """多个标签应同时被提取。"""
    tags = injector._extract_scenario_tags("VIP客户的小规模内部测试")
    assert "target:vip" in tags
    assert "scale:small" in tags


def test_extract_scenario_tags_empty(injector):
    """无匹配文本应返回空列表。"""
    tags = injector._extract_scenario_tags("普通文本无任何关键词")
    assert tags == []


# ========== _filter_by_scenario ==========


def test_filter_by_scenario_no_tags(injector, sample_checklist_items):
    """无场景标签时不应过滤。"""
    result = injector._filter_by_scenario(sample_checklist_items, [])
    assert len(result) == len(sample_checklist_items)


def test_filter_by_scenario_not_applies_when(injector):
    """命中 not_applies_when 的项应被排除。"""
    items = [
        ChecklistItem(item="A", source="test", not_applies_when=["target:price_sensitive"]),
        ChecklistItem(item="B", source="test"),
    ]
    result = injector._filter_by_scenario(items, ["target:price_sensitive"])
    assert len(result) == 1
    assert result[0].item == "B"


def test_filter_by_scenario_applies_when_required(injector):
    """applies_when 不匹配时应被排除。"""
    items = [
        ChecklistItem(item="A", source="test", applies_when=["target:vip"]),
        ChecklistItem(item="B", source="test", applies_when=["target:general"]),
    ]
    result = injector._filter_by_scenario(items, ["target:general"])
    assert len(result) == 1
    assert result[0].item == "B"


def test_filter_by_scenario_applies_when_match(injector):
    """applies_when 匹配时应保留。"""
    items = [
        ChecklistItem(item="A", source="test", applies_when=["target:vip"]),
    ]
    result = injector._filter_by_scenario(items, ["target:vip"])
    assert len(result) == 1
    assert result[0].item == "A"


def test_filter_by_scenario_combined_rules(injector):
    """组合规则测试：同时满足 applies_when 和 not_applies_when。"""
    items = [
        ChecklistItem(
            item="A",
            source="test",
            applies_when=["target:vip", "scale:large"],
            not_applies_when=["target:price_sensitive"],
        ),
        ChecklistItem(
            item="B",
            source="test",
            applies_when=["target:vip"],
            not_applies_when=["scale:large"],
        ),
    ]
    result = injector._filter_by_scenario(items, ["target:vip", "scale:large"])
    # A: applies_when 匹配 (vip+large)，not_applies_when 不匹配 -> 保留
    # B: applies_when 匹配 (vip)，not_applies_when 匹配 (large) -> 排除
    assert len(result) == 1
    assert result[0].item == "A"


# ========== _sort_by_relevance ==========


def test_sort_by_relevance_scenario_match_priority(injector):
    """场景匹配度应作为最高优先级排序因素。"""
    items = [
        ChecklistItem(
            item="A", source="test", applies_when=["target:vip"], hit_count=0, freshness_score=0.5
        ),
        ChecklistItem(
            item="B",
            source="test",
            applies_when=["target:general"],
            hit_count=10,
            freshness_score=1.0,
        ),
    ]
    sorted_items = injector._sort_by_relevance(items, ["target:vip"])
    assert sorted_items[0].item == "A"


def test_sort_by_relevance_hit_count_matters(injector):
    """hit_count 高的项应排在前面。"""
    items = [
        ChecklistItem(item="A", source="test", hit_count=1, freshness_score=1.0),
        ChecklistItem(item="B", source="test", hit_count=10, freshness_score=1.0),
    ]
    sorted_items = injector._sort_by_relevance(items, [])
    assert sorted_items[0].item == "B"


def test_sort_by_relevance_freshness_matters(injector):
    """freshness_score 高的项应排在前面。"""
    items = [
        ChecklistItem(item="A", source="test", freshness_score=0.2, hit_count=0),
        ChecklistItem(item="B", source="test", freshness_score=0.9, hit_count=0),
    ]
    sorted_items = injector._sort_by_relevance(items, [])
    assert sorted_items[0].item == "B"


def test_sort_by_relevance_severity_bonus(injector):
    """critical severity 应获得额外加分。"""
    items = [
        ChecklistItem(item="A", source="test", severity="low", hit_count=0, freshness_score=0.5),
        ChecklistItem(
            item="B", source="test", severity="critical", hit_count=0, freshness_score=0.5
        ),
    ]
    sorted_items = injector._sort_by_relevance(items, [])
    assert sorted_items[0].item == "B"


def test_sort_by_relevance_recency_bonus(injector):
    """最近7天内命中的项应获得加分。"""
    recent = (datetime.now() - timedelta(days=3)).isoformat()
    old = (datetime.now() - timedelta(days=20)).isoformat()
    items = [
        ChecklistItem(item="A", source="test", hit_count=1, last_hit=old, freshness_score=0.5),
        ChecklistItem(item="B", source="test", hit_count=1, last_hit=recent, freshness_score=0.5),
    ]
    sorted_items = injector._sort_by_relevance(items, [])
    assert sorted_items[0].item == "B"


def test_sort_by_relevance_no_items(injector):
    """空列表应返回空列表。"""
    result = injector._sort_by_relevance([], [])
    assert result == []


# ========== _apply_echo_chamber_breaker ==========


def test_apply_echo_chamber_breaker_no_persona(injector):
    """无画像时不应修改列表。"""
    injector.current_persona = None
    items = [ChecklistItem(item=f"item{i}", source="test") for i in range(6)]
    result = injector._apply_echo_chamber_breaker(items, [])
    assert result == items


def test_apply_echo_chamber_breaker_too_few_items(injector, monkeypatch):
    """项数少于5时不应修改。"""
    # 创建模拟画像
    mock_profile = MagicMock()
    mock_profile.value.correctness_vs_efficiency = 0.7
    mock_profile.value.insufficient_dimensions = []
    injector.current_persona = mock_profile

    items = [ChecklistItem(item=f"item{i}", source="test") for i in range(4)]
    result = injector._apply_echo_chamber_breaker(items, [])
    assert result == items


def test_apply_echo_chamber_breaker_mixes_items(injector, monkeypatch):
    """应将反画像项均匀插入到结果中。"""
    mock_profile = MagicMock()
    mock_profile.value.correctness_vs_efficiency = 0.7
    mock_profile.value.perfection_vs_completion = 0.5
    mock_profile.value.depth_vs_breadth = 0.5
    mock_profile.value.innovation_vs_safety = 0.5
    mock_profile.value.insufficient_dimensions = []
    injector.current_persona = mock_profile

    # monkeypatch _calc_persona_bonus 返回固定值
    monkeypatch.setattr(
        injector, "_calc_persona_bonus", lambda item: 0.5 if "explore" in item.item else 0.0
    )

    items = [
        ChecklistItem(item="exploit1", source="test"),
        ChecklistItem(item="exploit2", source="test"),
        ChecklistItem(item="exploit3", source="test"),
        ChecklistItem(item="exploit4", source="test"),
        ChecklistItem(item="explore1", source="test"),  # 反画像项
    ]
    result = injector._apply_echo_chamber_breaker(items, [])
    # explore1 应该被插入到某个位置（每5个位置插入1个）
    assert any("explore" in r.item for r in result)
    assert len(result) == len(items)


# ========== _calc_persona_bonus ==========


def test_calc_persona_bonus_correctness_priority(injector, monkeypatch):
    """correctness_vs_efficiency > 0.6 时 critical/high severity 应加分。"""
    mock_profile = MagicMock()
    mock_profile.value.correctness_vs_efficiency = 0.7
    mock_profile.value.perfection_vs_completion = 0.5
    mock_profile.value.depth_vs_breadth = 0.5
    mock_profile.value.innovation_vs_safety = 0.5
    mock_profile.value.insufficient_dimensions = []
    injector.current_persona = mock_profile

    critical_item = ChecklistItem(item="A", source="test", severity="critical")
    bonus = injector._calc_persona_bonus(critical_item)
    assert bonus > 0


def test_calc_persona_bonus_efficiency_priority(injector):
    """correctness_vs_efficiency < 0.4 时 low/medium severity 应加分。"""
    mock_profile = MagicMock()
    mock_profile.value.correctness_vs_efficiency = 0.3
    mock_profile.value.perfection_vs_completion = 0.5
    mock_profile.value.depth_vs_breadth = 0.5
    mock_profile.value.innovation_vs_safety = 0.5
    mock_profile.value.insufficient_dimensions = []
    injector.current_persona = mock_profile

    low_item = ChecklistItem(item="A", source="test", severity="low")
    bonus = injector._calc_persona_bonus(low_item)
    assert bonus > 0


def test_calc_persona_bonus_perfection_priority(injector):
    """perfection_vs_completion > 0.6 且 detail > 50 时应加分。"""
    mock_profile = MagicMock()
    mock_profile.value.correctness_vs_efficiency = 0.5
    mock_profile.value.perfection_vs_completion = 0.7
    mock_profile.value.depth_vs_breadth = 0.5
    mock_profile.value.innovation_vs_safety = 0.5
    mock_profile.value.insufficient_dimensions = []
    injector.current_persona = mock_profile

    detailed_item = ChecklistItem(item="A", source="test", detail="a" * 60)
    bonus = injector._calc_persona_bonus(detailed_item)
    assert bonus > 0


def test_calc_persona_bonus_depth_priority(injector):
    """depth_vs_breadth > 0.6 且 hit_count > 2 时应加分。"""
    mock_profile = MagicMock()
    mock_profile.value.correctness_vs_efficiency = 0.5
    mock_profile.value.perfection_vs_completion = 0.5
    mock_profile.value.depth_vs_breadth = 0.7
    mock_profile.value.innovation_vs_safety = 0.5
    mock_profile.value.insufficient_dimensions = []
    injector.current_persona = mock_profile

    deep_item = ChecklistItem(item="A", source="test", hit_count=5)
    bonus = injector._calc_persona_bonus(deep_item)
    assert bonus > 0


def test_calc_persona_bonus_innovation_priority(injector):
    """innovation_vs_safety > 0.6 且 freshness_score > 0.8 时应加分。"""
    mock_profile = MagicMock()
    mock_profile.value.correctness_vs_efficiency = 0.5
    mock_profile.value.perfection_vs_completion = 0.5
    mock_profile.value.depth_vs_breadth = 0.5
    mock_profile.value.innovation_vs_safety = 0.7
    mock_profile.value.insufficient_dimensions = []
    injector.current_persona = mock_profile

    fresh_item = ChecklistItem(item="A", source="test", freshness_score=0.9)
    bonus = injector._calc_persona_bonus(fresh_item)
    assert bonus > 0


def test_calc_persona_bonus_insufficient_dimension_skipped(injector):
    """标记为 insufficient 的维度不应参与计算。"""
    mock_profile = MagicMock()
    mock_profile.value.correctness_vs_efficiency = 0.7
    mock_profile.value.perfection_vs_completion = 0.5
    mock_profile.value.depth_vs_breadth = 0.5
    mock_profile.value.innovation_vs_safety = 0.5
    mock_profile.value.insufficient_dimensions = ["correctness_vs_efficiency"]
    injector.current_persona = mock_profile

    critical_item = ChecklistItem(item="A", source="test", severity="critical")
    bonus = injector._calc_persona_bonus(critical_item)
    # correctness_vs_efficiency 被标记为 insufficient，不应加分
    assert bonus == 0.0


def test_calc_persona_bonus_no_persona(injector):
    """无画像时应返回 0。"""
    injector.current_persona = None
    item = ChecklistItem(item="A", source="test")
    assert injector._calc_persona_bonus(item) == 0.0


# ========== format_for_context ==========


def test_format_for_context_basic(injector, sample_loaded_knowledge):
    """基本格式化应包含任务类型和 checklist。"""
    text = injector.format_for_context(sample_loaded_knowledge)
    assert "[Knowledge Loaded: coding/debug v1]" in text
    assert "检查并发安全" in text
    assert "优化查询性能" in text
    assert "上次复盘要点" in text
    assert "[装载统计]" in text


def test_format_for_context_empty(injector):
    """空知识应返回空字符串。"""
    lk = LoadedKnowledge(
        task_type="coding",
        subtype="debug",
        version=1,
        checklist=[],
        lessons_summary="",
        loaded_at="2024-01-01T00:00:00",
    )
    assert injector.format_for_context(lk) == ""


def test_format_for_context_none(injector):
    """None 输入应返回空字符串。"""
    assert injector.format_for_context(None) == ""


def test_format_for_context_compact_notice(injector):
    """is_compact=True 时应显示压缩提示。"""
    lk = LoadedKnowledge(
        task_type="coding",
        subtype="debug",
        version=1,
        checklist=[ChecklistItem(item="A", source="test")],
        lessons_summary="",
        loaded_at="2024-01-01T00:00:00",
        is_compact=True,
    )
    text = injector.format_for_context(lk)
    assert "仅显示最关键的10条" in text


def test_format_for_context_minimal_mode(injector, monkeypatch):
    """perfection_vs_completion < 0.4 时应进入 minimal 模式。"""
    mock_profile = MagicMock()
    mock_profile.value.perfection_vs_completion = 0.3
    injector.current_persona = mock_profile

    lk = LoadedKnowledge(
        task_type="coding",
        subtype="debug",
        version=1,
        checklist=[
            ChecklistItem(item="A", source="test", severity="low"),
            ChecklistItem(item="B", source="test", severity="critical"),
        ],
        lessons_summary="summary",
        loaded_at="2024-01-01T00:00:00",
    )
    text = injector.format_for_context(lk)
    # minimal 模式不显示 lessons_summary
    assert "上次复盘要点" not in text
    # 只保留 critical/high
    assert "B" in text


def test_format_for_context_thorough_mode(injector, monkeypatch):
    """perfection_vs_completion > 0.6 时应进入 thorough 模式。"""
    mock_profile = MagicMock()
    mock_profile.value.perfection_vs_completion = 0.7
    injector.current_persona = mock_profile

    lk = LoadedKnowledge(
        task_type="coding",
        subtype="debug",
        version=1,
        checklist=[
            ChecklistItem(item="A", source="test", detail="详细说明内容"),
        ],
        lessons_summary="",
        loaded_at="2024-01-01T00:00:00",
    )
    text = injector.format_for_context(lk)
    # thorough 模式显示完整 detail
    assert "详情: 详细说明内容" in text


def test_format_for_context_usage_stats(injector):
    """有 hit_count/ignore_count 的项应显示统计。"""
    lk = LoadedKnowledge(
        task_type="coding",
        subtype="debug",
        version=1,
        checklist=[
            ChecklistItem(item="A", source="test", hit_count=3, ignore_count=1),
        ],
        lessons_summary="",
        loaded_at="2024-01-01T00:00:00",
    )
    text = injector.format_for_context(lk)
    assert "[H:3/I:1]" in text


# ========== mark_checklist_used ==========


def test_mark_checklist_used_success(injector):
    """成功标记使用应更新 hit_count。"""
    task_dir = injector.RETROSPECTIVES_DIR / "coding"
    task_dir.mkdir(parents=True, exist_ok=True)

    retro_file = task_dir / "debug-v1.md"
    retro_file.write_text(
        "---\n"
        "version: 1\n"
        "checklist:\n"
        "  - item: 检查边界\n"
        "    source: test\n"
        "    hit_count: 2\n"
        "---\n"
        "# Body\n",
        encoding="utf-8",
    )

    result = injector.mark_checklist_used("coding", "debug", 0, used=True)
    assert result is True

    # 验证文件已更新
    content = retro_file.read_text(encoding="utf-8")
    assert "hit_count: 3" in content
    assert "last_hit" in content


def test_mark_checklist_used_no_file(injector):
    """无复盘文件时应返回 False。"""
    result = injector.mark_checklist_used("nonexistent", "subtype", 0)
    assert result is False


def test_mark_checklist_used_index_out_of_range(injector):
    """索引越界时应返回 False。"""
    task_dir = injector.RETROSPECTIVES_DIR / "coding"
    task_dir.mkdir(parents=True, exist_ok=True)

    retro_file = task_dir / "debug-v1.md"
    retro_file.write_text(
        "---\nversion: 1\nchecklist:\n  - item: 检查边界\n    source: test\n---\n",
        encoding="utf-8",
    )

    result = injector.mark_checklist_used("coding", "debug", 5, used=True)
    assert result is False


def test_mark_checklist_used_not_used(injector):
    """used=False 时不应更新 hit_count。"""
    task_dir = injector.RETROSPECTIVES_DIR / "coding"
    task_dir.mkdir(parents=True, exist_ok=True)

    retro_file = task_dir / "debug-v1.md"
    original_content = (
        "---\n"
        "version: 1\n"
        "checklist:\n"
        "  - item: 检查边界\n"
        "    source: test\n"
        "    hit_count: 2\n"
        "---\n"
    )
    retro_file.write_text(original_content, encoding="utf-8")

    result = injector.mark_checklist_used("coding", "debug", 0, used=False)
    assert result is True

    content = retro_file.read_text(encoding="utf-8")
    # hit_count 不应变化
    assert "hit_count: 2" in content


# ========== _generate_checklist_from_page ==========


def test_generate_checklist_from_page_keywords(injector):
    """应从关键词生成 checklist。"""
    fm = {"关键词": ["并发", "性能", "安全"]}
    items = injector._generate_checklist_from_page(fm, "")
    assert len(items) == 3
    assert items[0]["item"] == "相关知识: 并发"


def test_generate_checklist_from_page_triggers(injector):
    """应从触发器生成 checklist。"""
    fm = {"触发器": ["审计", "部署"]}
    items = injector._generate_checklist_from_page(fm, "")
    assert len(items) == 2
    assert items[0]["item"] == "触发场景: 审计"


def test_generate_checklist_from_page_body_headings(injector):
    """应从正文标题提取反模式/缺陷/注意。"""
    body = "## 常见缺陷：空指针\n## 最佳实践：先验证\n## 普通标题\n## 风险提示：数据丢失"
    items = injector._generate_checklist_from_page({}, body)
    assert any("空指针" in i["item"] for i in items)
    assert any("数据丢失" in i["item"] for i in items)
    assert any("先验证" in i["item"] for i in items)


def test_generate_checklist_from_page_empty(injector):
    """空输入应返回空列表。"""
    items = injector._generate_checklist_from_page({}, "")
    assert items == []


# ========== _find_wiki_fallback ==========


def test_find_wiki_fallback_no_dir(injector):
    """目录不存在时应返回 None。"""
    result = injector._find_wiki_fallback("coding")
    # 06-Retrospectives 存在但为空
    assert result is None


def test_find_wiki_fallback_by_task_type(injector):
    """文件名含 task_type 时应匹配。"""
    retro_dir = injector.WIKI_BASE / "06-Retrospectives"
    retro_dir.mkdir(parents=True, exist_ok=True)

    f = retro_dir / "coding_反模式.md"
    f.write_text("---\n---\n", encoding="utf-8")

    result = injector._find_wiki_fallback("coding")
    assert result is not None
    assert result.name == "coding_反模式.md"


def test_find_wiki_fallback_by_page_type(injector):
    """frontmatter 类型匹配时应返回。"""
    retro_dir = injector.WIKI_BASE / "06-Retrospectives"
    retro_dir.mkdir(parents=True, exist_ok=True)

    f = retro_dir / "some_page.md"
    f.write_text("---\n类型: coding_review\n---\n", encoding="utf-8")

    result = injector._find_wiki_fallback("coding")
    assert result is not None


def test_find_wiki_fallback_ignores_underscore(injector):
    """以 _ 开头的文件应被忽略。"""
    retro_dir = injector.WIKI_BASE / "06-Retrospectives"
    retro_dir.mkdir(parents=True, exist_ok=True)

    f = retro_dir / "_draft.md"
    f.write_text("---\n---\n", encoding="utf-8")

    result = injector._find_wiki_fallback("coding")
    assert result is None


# ========== _get_checklist_for_type / _get_checklist_from_files ==========


def test_get_checklist_from_files_no_match(injector):
    """无匹配文件时应返回空列表。"""
    result = injector._get_checklist_from_files("nonexistent")
    assert result == []


def test_get_checklist_from_files_with_match(injector):
    """有匹配文件时应返回解析后的 checklist。"""
    retro_dir = injector.WIKI_BASE / "06-Retrospectives"
    retro_dir.mkdir(parents=True, exist_ok=True)

    f = retro_dir / "coding_反模式.md"
    f.write_text(
        "---\n"
        "checklist:\n"
        "  - item: 检查并发\n"
        "    source: wiki\n"
        "    severity: high\n"
        "---\n",
        encoding="utf-8",
    )

    result = injector._get_checklist_from_files("coding")
    assert len(result) == 1
    assert result[0].item == "检查并发"
    assert result[0].severity == "high"


# ========== _build_fallback_lessons_summary ==========


def test_build_fallback_lessons_summary_empty(injector):
    """空输入应返回空字符串。"""
    result = injector._build_fallback_lessons_summary([])
    assert result == ""


def test_build_fallback_lessons_summary_with_items(injector):
    """有 items 时应生成摘要。"""
    items = [
        ChecklistItem(item="检查并发安全", source="wiki"),
        ChecklistItem(item="优化性能", source="wiki"),
    ]
    result = injector._build_fallback_lessons_summary(items)
    assert "未命中专用复盘文件" in result
    assert "检查并发安全" in result
    assert "优化性能" in result


def test_build_fallback_lessons_summary_deduplicates(injector):
    """重复项应被去重。"""
    items = [
        ChecklistItem(item="相同项", source="a"),
        ChecklistItem(item="相同项", source="b"),
    ]
    result = injector._build_fallback_lessons_summary(items)
    # 只应出现一次
    assert result.count("相同项") == 1


# ========== _warm_checklist_cache ==========


def test_warm_checklist_cache_creates_db(injector):
    """_warm_checklist_cache 应创建 SQLite 数据库。"""
    # injector 初始化时已调用 _warm_checklist_cache
    assert injector._cache_db_path.exists()


def test_warm_checklist_cache_scans_files(injector):
    """应扫描 Wiki 文件并填充缓存。"""
    concepts_dir = injector.WIKI_BASE / "04-Concepts"
    concepts_dir.mkdir(parents=True, exist_ok=True)

    f = concepts_dir / "test_concept.md"
    f.write_text(
        "---\n"
        "类型: retrospective\n"
        "名称: 测试概念\n"
        "task_type: coding\n"
        "关键词:\n  - 测试\n  - 概念\n"
        "---\n",
        encoding="utf-8",
    )

    # 重新初始化以触发 _warm_checklist_cache
    injector._warm_checklist_cache()

    # 验证缓存中有数据
    import sqlite3

    conn = sqlite3.connect(str(injector._cache_db_path))
    cursor = conn.execute("SELECT COUNT(*) FROM checklist_cache")
    count = cursor.fetchone()[0]
    conn.close()
    assert count >= 1


# ========== _load_persona ==========


def test_load_persona_success(injector, monkeypatch):
    """成功加载画像时应设置 current_persona。"""
    mock_profile = MagicMock()
    mock_store = MagicMock()
    mock_store.load_persona.return_value = (mock_profile, None)
    monkeypatch.setattr(injector, "persona_store", mock_store)

    injector._load_persona()
    assert injector.current_persona is mock_profile


def test_load_persona_failure(injector, monkeypatch):
    """加载失败时应设置 current_persona 为 None。"""
    mock_store = MagicMock()
    mock_store.load_persona.side_effect = RuntimeError("加载失败")
    monkeypatch.setattr(injector, "persona_store", mock_store)

    injector._load_persona()
    assert injector.current_persona is None


def test_load_persona_does_not_hide_programming_errors(injector, monkeypatch):
    mock_store = MagicMock()
    mock_store.load_persona.side_effect = AssertionError("broken persona contract")
    monkeypatch.setattr(injector, "persona_store", mock_store)

    with pytest.raises(AssertionError, match="broken persona contract"):
        injector._load_persona()


# ========== BEHAVIOR_CONSTRAINTS 常量 ==========


def test_behavior_constraints_not_empty():
    """BEHAVIOR_CONSTRAINTS 不应为空。"""
    assert len(BEHAVIOR_CONSTRAINTS) > 0


def test_behavior_constraints_have_severity():
    """所有行为约束应有 severity 字段。"""
    for item in BEHAVIOR_CONSTRAINTS:
        assert item.severity in ("critical", "high", "medium", "low")


def test_behavior_constraints_have_trigger_keywords():
    """所有行为约束应有 trigger_keywords。"""
    for item in BEHAVIOR_CONSTRAINTS:
        assert len(item.trigger_keywords) > 0


# ========== P117: knowledge_gaps.md 消费 ==========


def test_load_knowledge_gaps_parsing(injector, tmp_path):
    """_load_knowledge_gaps 应正确解析 EvolutionTracker 生成的 markdown。"""
    gaps_file = tmp_path / "06-Retrospectives" / "knowledge_gaps.md"
    gaps_file.write_text(
        "# 知识缺口预加载提示\n\n"
        "更新时间: 2026-06-18T00:00:00\n"
        "缺口数量: 2\n\n"
        "> 本文件由 EvolutionTracker 自动生成，供 KIA preflight_inject 加载。\n\n"
        "## Rust 异步运行时\n\n"
        "- **缺口类型**: unrecorded\n"
        "- **详情**: 用户近期多次提到 tokio 但 Wiki 无系统记录\n"
        "- **建议引导方向**: 询问用户关于 `Rust 异步运行时` 的最新进展或实践经验\n\n"
        "---\n\n"
        "## 使用方式\n\n"
        "1. `preflight_inject` 在加载知识时读取本文件\n",
        encoding="utf-8",
    )

    gaps = injector._load_knowledge_gaps()
    assert len(gaps) == 1
    assert gaps[0].item == "知识缺口：Rust 异步运行时"
    assert gaps[0].source == "06-Retrospectives/knowledge_gaps.md"
    assert "unrecorded" in gaps[0].detail


def test_filter_relevant_gaps_by_context(injector):
    """_filter_relevant_gaps 根据上下文命中 entity 关键词。"""
    gaps = [
        ChecklistItem(item="知识缺口：Rust 异步运行时", source="gaps", severity="medium"),
        ChecklistItem(item="知识缺口：Python GIL", source="gaps", severity="medium"),
    ]
    matched = injector._filter_relevant_gaps(gaps, "coding", "async", "我在用 tokio 写异步服务")
    assert len(matched) == 1
    assert "Rust" in matched[0].item


def test_filter_relevant_gaps_by_task_type(injector):
    """_filter_relevant_gaps 在上下文无命中时回退匹配 task_type/subtype。"""
    gaps = [
        ChecklistItem(item="知识缺口：Python GIL", source="gaps", severity="medium"),
    ]
    matched = injector._filter_relevant_gaps(gaps, "python", "gil", "")
    assert len(matched) == 1


def test_merge_knowledge_gaps_prepends(injector):
    """_merge_knowledge_gaps 把缺口项放到 checklist 前端并去重。"""
    existing = [ChecklistItem(item="已有项", source="retro", severity="high")]
    gaps = [ChecklistItem(item="知识缺口：新领域", source="gaps", severity="medium")]
    merged = injector._merge_knowledge_gaps(existing, gaps)
    assert len(merged) == 2
    assert merged[0].item == "知识缺口：新领域"


def test_load_full_includes_knowledge_gaps(injector, tmp_path):
    """_load_full 无专用复盘文件时应把相关知识缺口合并进结果。"""
    gaps_file = tmp_path / "06-Retrospectives" / "knowledge_gaps.md"
    gaps_file.write_text(
        "# 知识缺口预加载提示\n\n"
        "## Rust 异步运行时\n\n"
        "- **缺口类型**: unrecorded\n"
        "- **详情**: 用户近期多次提到 tokio\n"
        "- **建议引导方向**: 询问用户关于 `Rust 异步运行时` 的实践经验\n\n"
        "---\n\n"
        "## 使用方式\n",
        encoding="utf-8",
    )

    result = injector._load_full("coding", "async", "我在用 tokio 写异步服务")
    assert result is not None
    items = [i.item for i in result.checklist]
    assert any("知识缺口：Rust 异步运行时" in i for i in items)
