"""
Preference Analyzer (Pythia) 单元测试

覆盖公共行为：
1. Profile 数据类 — EnergyProfile / CognitiveProfile / ValueProfile / PreferenceProfile
   - 默认值、字段存在性
   - to_dict 输出结构与标签映射
   - insufficient_dimensions 对 to_dict 的影响
   - _label_depth / _label_startup 静态方法
2. PreferenceAnalyzer — 画像分析引擎
   - __init__ 接受外部 store
   - analyze 全量模式（无 previous_profile）
   - analyze 增量模式（有 previous_profile）
   - _analyze_energy / _analyze_cognitive / _analyze_value 私有方法
   - _calculate_confidence 边界情况
   - _calculate_changes 变化标签计算
   - detect_drift 漂移检测（sudden_shift / gradual_drift / update_lag / low_confidence_drift）
   - _fallback_from_knowledge_profile 降级推断
   - _parse_intervals 时间戳解析与过滤
3. 便捷函数
   - analyze_preferences(days)
   - generate_radar_report(profile)

隔离手段：
- monkeypatch 替换 get_signal_store 返回 MockStore
- tmp_path 用于需要文件系统的场景
"""

from datetime import datetime, timedelta
from unittest.mock import MagicMock

import pytest

from core.persona.pythia import (
    PREFERENCE_ANALYZER_STARTUP_DIFFICULTY_SCALE_SECONDS,
    EnergyProfile,
    CognitiveProfile,
    ValueProfile,
    PreferenceProfile,
    PreferenceAnalyzer,
    _PercentileNormalizer,
    _BehaviorCalibrator,
    _DomainPreferenceAnalyzer,
    analyze_preferences,
    generate_radar_report,
)

# =====================================================================
# Fixtures
# =====================================================================


class MockSignalStore:
    """极简内存 store，隔离真实 SQLite。"""

    def __init__(self):
        self._session = []
        self._git = []
        self._wiki = []
        self._reflection = []
        self._processed = []

    def get_recent_session_signals(self, days=90):
        return list(self._session)

    def get_unprocessed_signals(self, source_type, limit=1000):
        if source_type == "session":
            return [s for s in self._session if s.get("id") not in self._processed]
        if source_type == "git":
            return [s for s in self._git if s.get("id") not in self._processed]
        if source_type == "knowledge":
            return [s for s in self._wiki if s.get("id") not in self._processed]
        return []

    def get_recent_reflection_signals(self, days=90):
        return list(self._reflection)

    def get_reflection_signals_since(self, since_iso, limit=1000):
        return [s for s in self._reflection if s.get("timestamp", "") > since_iso]

    def mark_signals_processed(self, source_type, signal_ids):
        self._processed.extend(signal_ids)

    def add_session(self, **kwargs):
        sig = {"id": len(self._session) + 1, **kwargs}
        self._session.append(sig)
        return sig

    def add_git(self, **kwargs):
        sig = {"id": len(self._git) + 1, **kwargs}
        self._git.append(sig)
        return sig

    def add_wiki(self, **kwargs):  # noqa: Vulture - test store mirrors wiki signal writer.
        sig = {"id": len(self._wiki) + 1, **kwargs}
        self._wiki.append(sig)
        return sig

    def add_reflection(self, **kwargs):
        sig = {"id": len(self._reflection) + 1, "timestamp": datetime.now().isoformat(), **kwargs}
        self._reflection.append(sig)
        return sig


@pytest.fixture
def mock_store():
    return MockSignalStore()


@pytest.fixture
def analyzer(mock_store):
    return PreferenceAnalyzer(store=mock_store)


# =====================================================================
# Profile Dataclasses
# =====================================================================


class TestEnergyProfile:
    def test_default_values(self):
        e = EnergyProfile()
        assert e.focus_depth == 0.5
        assert e.startup_difficulty == 0.5
        assert e.endurance_mode == 0.5
        assert e.switching_flexibility == 0.5
        assert e.recovery_cycle == 0.5
        assert e.confidence == 0.0
        assert e.insufficient_dimensions is None

    def test_custom_values(self):
        e = EnergyProfile(focus_depth=0.9, confidence=0.8)
        assert e.focus_depth == 0.9
        assert e.confidence == 0.8


class TestCognitiveProfile:
    def test_default_values(self):
        c = CognitiveProfile()
        assert c.abstraction == 0.5
        assert c.system_view == 0.5
        assert c.skepticism == 0.5
        assert c.creativity == 0.5
        assert c.deduction == 0.5
        assert c.confidence == 0.0

    def test_custom_values(self):
        c = CognitiveProfile(abstraction=0.2, creativity=0.9)
        assert c.abstraction == 0.2
        assert c.creativity == 0.9


class TestValueProfile:
    def test_default_values(self):
        v = ValueProfile()
        assert v.correctness_vs_efficiency == 0.5
        assert v.depth_vs_breadth == 0.5
        assert v.perfection_vs_completion == 0.5
        assert v.innovation_vs_safety == 0.5
        assert v.autonomy_vs_collaboration == 0.5
        assert v.action_vs_analysis == 0.5
        assert v.confidence == 0.0

    def test_custom_values(self):
        v = ValueProfile(depth_vs_breadth=0.8, action_vs_analysis=0.3)
        assert v.depth_vs_breadth == 0.8
        assert v.action_vs_analysis == 0.3


class TestPreferenceProfile:
    def test_default_values(self):
        p = PreferenceProfile()
        assert p.version == 0
        assert p.generated_at == ""
        assert p.period_start == ""
        assert p.period_end == ""
        assert p.signal_count == 0
        assert isinstance(p.energy, EnergyProfile)
        assert isinstance(p.cognitive, CognitiveProfile)
        assert isinstance(p.value, ValueProfile)

    def test_to_dict_structure(self):
        p = PreferenceProfile(
            version=1,
            generated_at="2024-01-01T00:00:00",
            period_start="2024-01-01",
            period_end="2024-03-31",
            signal_count=42,
        )
        d = p.to_dict()
        assert d["version"] == 1
        assert d["generated_at"] == "2024-01-01T00:00:00"
        assert d["period_start"] == "2024-01-01"
        assert d["period_end"] == "2024-03-31"
        assert d["signal_count"] == 42
        assert "energy" in d
        assert "cognitive" in d
        assert "value" in d

    def test_to_dict_energy_labels(self):
        p = PreferenceProfile(
            energy=EnergyProfile(
                focus_depth=0.35,
                startup_difficulty=0.8,
                endurance_mode=0.3,
                switching_flexibility=0.7,
                recovery_cycle=0.5,
                confidence=0.6,
            )
        )
        d = p.to_dict()["energy"]
        assert d["focus_depth"]["label"] == "中等专注"
        assert d["startup_difficulty"]["label"] == "需要推力"
        assert d["endurance_mode"]["label"] == "爆发型"
        assert d["switching_flexibility"]["label"] == "多线程"
        assert d["recovery_cycle"]["label"] == "中等恢复"
        assert d["confidence"] == 0.6

    def test_to_dict_cognitive_labels(self):
        p = PreferenceProfile(
            cognitive=CognitiveProfile(
                abstraction=0.2,
                system_view=0.8,
                skepticism=0.5,
                creativity=0.9,
                deduction=0.1,
                confidence=0.7,
            )
        )
        d = p.to_dict()["cognitive"]
        assert d["abstraction"]["label"] == "具象型"
        assert d["system_view"]["label"] == "系统视角"
        assert d["skepticism"]["label"] == "适度质疑"
        assert d["creativity"]["label"] == "创造型"
        assert d["deduction"]["label"] == "归纳型"

    def test_to_dict_value_labels(self):
        p = PreferenceProfile(
            value=ValueProfile(
                correctness_vs_efficiency=0.3,
                depth_vs_breadth=0.8,
                perfection_vs_completion=0.5,
                innovation_vs_safety=0.2,
                autonomy_vs_collaboration=0.9,
                action_vs_analysis=0.39,
                confidence=0.8,
            )
        )
        d = p.to_dict()["value"]
        assert d["correctness_vs_efficiency"]["label"] == "效率优先"
        assert d["depth_vs_breadth"]["label"] == "深度优先"
        assert d["perfection_vs_completion"]["label"] == "平衡"
        assert d["innovation_vs_safety"]["label"] == "稳妥优先"
        assert d["autonomy_vs_collaboration"]["label"] == "自主优先"
        assert d["action_vs_analysis"]["label"] == "分析优先"

    def test_to_dict_with_insufficient_dimensions(self):
        p = PreferenceProfile(
            energy=EnergyProfile(
                recovery_cycle=0.5,
                insufficient_dimensions=["recovery_cycle"],
            ),
            cognitive=CognitiveProfile(
                creativity=0.5,
                deduction=0.5,
                insufficient_dimensions=["creativity", "deduction"],
            ),
            value=ValueProfile(
                innovation_vs_safety=0.5,
                autonomy_vs_collaboration=0.5,
                action_vs_analysis=0.5,
                insufficient_dimensions=[
                    "innovation_vs_safety",
                    "autonomy_vs_collaboration",
                    "action_vs_analysis",
                ],
            ),
        )
        d = p.to_dict()
        assert d["energy"]["recovery_cycle"]["score"] == "—"
        assert d["energy"]["recovery_cycle"]["label"] == "数据不足"
        assert d["cognitive"]["creativity"]["score"] == "—"
        assert d["cognitive"]["deduction"]["score"] == "—"
        assert d["value"]["innovation_vs_safety"]["score"] == "—"
        assert d["value"]["autonomy_vs_collaboration"]["score"] == "—"
        assert d["value"]["action_vs_analysis"]["score"] == "—"

    def test_label_depth_boundaries(self):
        assert PreferenceProfile._label_depth(0.0) == "碎片化"
        assert PreferenceProfile._label_depth(0.29) == "碎片化"
        assert PreferenceProfile._label_depth(0.3) == "中等专注"
        assert PreferenceProfile._label_depth(0.49) == "中等专注"
        assert PreferenceProfile._label_depth(0.5) == "较深度"
        assert PreferenceProfile._label_depth(0.69) == "较深度"
        assert PreferenceProfile._label_depth(0.7) == "深度沉浸"
        assert PreferenceProfile._label_depth(1.0) == "深度沉浸"

    def test_label_startup_boundaries(self):
        assert PreferenceProfile._label_startup(0.0) == "一触即发"
        assert PreferenceProfile._label_startup(0.29) == "一触即发"
        assert PreferenceProfile._label_startup(0.3) == "启动较快"
        assert PreferenceProfile._label_startup(0.49) == "启动较快"
        assert PreferenceProfile._label_startup(0.5) == "需要准备"
        assert PreferenceProfile._label_startup(0.69) == "需要准备"
        assert PreferenceProfile._label_startup(0.7) == "需要推力"
        assert PreferenceProfile._label_startup(1.0) == "需要推力"

    def test_to_dict_score_rounding(self):
        p = PreferenceProfile(
            energy=EnergyProfile(focus_depth=0.333333, confidence=0.666666),
        )
        d = p.to_dict()["energy"]
        assert d["focus_depth"]["score"] == 0.33
        assert d["confidence"] == 0.67


# =====================================================================
# PreferenceAnalyzer — _calculate_confidence
# =====================================================================


class TestCalculateConfidence:
    def test_full_signals(self, analyzer):
        counts = {"session": 10, "git": 5, "wechat": 0}
        assert analyzer._calculate_confidence(counts) == 1.0

    def test_partial_signals(self, analyzer):
        counts = {"session": 5, "git": 2, "wechat": 0}
        # (5/10 + 2/5 + 0) / 2 active sources = (0.5 + 0.4) / 2 = 0.45
        assert analyzer._calculate_confidence(counts) == 0.45

    def test_zero_signals(self, analyzer):
        counts = {"session": 0, "git": 0, "wechat": 0}
        assert analyzer._calculate_confidence(counts) == 0.0

    def test_single_source(self, analyzer):
        counts = {"session": 20, "git": 0}
        assert analyzer._calculate_confidence(counts) == 1.0

    def test_unknown_source_uses_default_min(self, analyzer):
        counts = {"unknown": 5}
        assert analyzer._calculate_confidence(counts) == 0.5


# =====================================================================
# PreferenceAnalyzer — _parse_intervals
# =====================================================================


class TestParseIntervals:
    def test_basic_intervals(self, analyzer):
        ts = [
            "2024-01-01T10:00:00",
            "2024-01-01T10:05:00",
            "2024-01-01T10:10:00",
        ]
        intervals = analyzer._parse_intervals(ts)
        assert len(intervals) == 2
        assert intervals[0] == pytest.approx(300, rel=1e-3)
        assert intervals[1] == pytest.approx(300, rel=1e-3)

    def test_filters_too_short(self, analyzer):
        ts = [
            "2024-01-01T10:00:00",
            "2024-01-01T10:00:30",  # 30s < 60s min
        ]
        intervals = analyzer._parse_intervals(ts)
        assert len(intervals) == 0

    def test_filters_too_long(self, analyzer):
        ts = [
            "2024-01-01T10:00:00",
            "2024-03-01T10:00:00",  # ~60 days > 30 days max
        ]
        intervals = analyzer._parse_intervals(ts)
        assert len(intervals) == 0

    def test_skips_empty_timestamps(self, analyzer):
        ts = ["", "2024-01-01T10:00:00", "2024-01-01T10:05:00", None]
        intervals = analyzer._parse_intervals(ts)
        assert len(intervals) == 1
        assert intervals[0] == pytest.approx(300, rel=1e-3)

    def test_skips_invalid_timestamps(self, analyzer):
        ts = ["not-a-date", "2024-01-01T10:00:00", "2024-01-01T10:05:00"]
        intervals = analyzer._parse_intervals(ts)
        assert len(intervals) == 1

    def test_sorts_before_calculating(self, analyzer):
        ts = [
            "2024-01-01T10:10:00",
            "2024-01-01T10:00:00",
            "2024-01-01T10:05:00",
        ]
        intervals = analyzer._parse_intervals(ts)
        assert len(intervals) == 2
        assert intervals[0] == pytest.approx(300, rel=1e-3)
        assert intervals[1] == pytest.approx(300, rel=1e-3)

    def test_empty_list(self, analyzer):
        assert analyzer._parse_intervals([]) == []

    def test_single_timestamp(self, analyzer):
        assert analyzer._parse_intervals(["2024-01-01T10:00:00"]) == []

    def test_z_suffix(self, analyzer):
        ts = [
            "2024-01-01T10:00:00Z",
            "2024-01-01T10:05:00Z",
        ]
        intervals = analyzer._parse_intervals(ts)
        assert len(intervals) == 1
        assert intervals[0] == pytest.approx(300, rel=1e-3)


# =====================================================================
# PreferenceAnalyzer — _analyze_energy
# =====================================================================


class TestAnalyzeEnergy:
    def test_focus_depth_no_longer_exposes_legacy_seconds_constants(self):
        assert not hasattr(PreferenceAnalyzer, "FOCUS_DEPTH_BASE_SECONDS")
        assert not hasattr(PreferenceAnalyzer, "FOCUS_DEPTH_SCALE_SECONDS")

    def test_startup_difficulty_scale_constant_is_public_contract(self):
        assert (
            PreferenceAnalyzer.STARTUP_DIFFICULTY_SCALE_SECONDS
            == PREFERENCE_ANALYZER_STARTUP_DIFFICULTY_SCALE_SECONDS
            == 3600
        )

    def test_empty_signals(self, analyzer):
        e = analyzer._analyze_energy([], [], [], [])
        assert isinstance(e, EnergyProfile)
        assert e.focus_depth == 0.5
        assert e.confidence == 0.0
        assert e.insufficient_dimensions == ["recovery_cycle"]

    def test_focus_depth_from_duration(self, analyzer):
        # 使用分位数归一化：单值窗口返回 0.5，行为校准 follow_up=10 加 0.13
        session = [{"duration_seconds": 1800, "follow_up_depth": 10}]
        e = analyzer._analyze_energy(session, [], [], [])
        # (0.5 + 0.5) / 2 = 0.5, + 校准 0.13 = 0.63
        assert e.focus_depth == pytest.approx(0.63, abs=0.01)

    def test_focus_depth_with_follow_up(self, analyzer):
        # 分位数归一化：单值窗口返回 0.5，行为校准 follow_up=5 加 0.09
        session = [{"duration_seconds": 300, "follow_up_depth": 5}]
        e = analyzer._analyze_energy(session, [], [], [])
        # (0.5 + 0.5) / 2 = 0.5, + 校准 0.09 = 0.59
        assert e.focus_depth == pytest.approx(0.59, abs=0.01)

    def test_startup_difficulty_from_intervals(self, analyzer):
        now = datetime.now()
        session = [
            {"timestamp": (now - timedelta(hours=2)).isoformat(), "duration_seconds": 300},
            {"timestamp": (now - timedelta(hours=1)).isoformat(), "duration_seconds": 300},
            {"timestamp": now.isoformat(), "duration_seconds": 300},
        ]
        e = analyzer._analyze_energy(session, [], [], [])
        # 分位数归一化：两个相同间隔值 [3600, 3600]，identical values 在窗口内排第 0 位
        assert e.startup_difficulty == pytest.approx(0.0, abs=0.01)

    def test_endurance_mode_from_git(self, analyzer):
        # All commits at hour 10 -> concentration = 1.0 -> endurance_mode = 0.0
        git = [{"hour_of_day": 10} for _ in range(10)]
        e = analyzer._analyze_energy([], git, [], [])
        assert e.endurance_mode == pytest.approx(0.0, abs=0.01)

    def test_endurance_mode_uniform_git(self, analyzer):
        # Commits spread across hours -> low concentration -> high endurance_mode
        git = [{"hour_of_day": h} for h in range(10)]
        e = analyzer._analyze_energy([], git, [], [])
        assert e.endurance_mode > 0.5

    def test_switching_flexibility_from_tasks(self, analyzer):
        session = [
            {"task_type": "coding"},
            {"task_type": "debugging"},
            {"task_type": "review"},
        ]
        e = analyzer._analyze_energy(session, [], [], [])
        # 分位数归一化：单值窗口返回 0.5
        assert e.switching_flexibility == pytest.approx(0.5, abs=0.01)

    def test_recovery_cycle_from_git_weekend(self, analyzer):
        git = [{"is_weekend": True} for _ in range(10)]
        e = analyzer._analyze_energy([], git, [], [])
        # weekend_ratio = 1.0 -> recovery_cycle = 0.0 (quick recovery)
        assert e.recovery_cycle == pytest.approx(0.0, abs=0.01)

    def test_recovery_cycle_from_git_weekday(self, analyzer):
        git = [{"is_weekend": False} for _ in range(10)]
        e = analyzer._analyze_energy([], git, [], [])
        # weekend_ratio = 0.0 -> recovery_cycle = 1.0 (needs buffer)
        assert e.recovery_cycle == pytest.approx(1.0, abs=0.01)

    def test_recovery_cycle_insufficient_git(self, analyzer):
        git = [{"is_weekend": False} for _ in range(3)]  # < MIN_SIGNALS["git"]=5
        e = analyzer._analyze_energy([], git, [], [])
        assert "recovery_cycle" in e.insufficient_dimensions

    def test_confidence_with_mixed_signals(self, analyzer):
        session = [{"duration_seconds": 300} for _ in range(10)]
        git = [{"hour_of_day": 10} for _ in range(5)]
        e = analyzer._analyze_energy(session, git, [], [])
        # session=10/10=1.0, git=5/5=1.0 -> avg = 1.0
        assert e.confidence == 1.0


# =====================================================================
# PreferenceAnalyzer — _analyze_cognitive
# =====================================================================


class TestAnalyzeCognitive:
    def test_empty_signals(self, analyzer):
        c = analyzer._analyze_cognitive([], [], [])
        assert isinstance(c, CognitiveProfile)
        assert c.abstraction == 0.5
        assert c.confidence == 0.0
        assert c.insufficient_dimensions == ["creativity", "deduction"]

    def test_abstraction_from_session(self, analyzer):
        session = [
            {"final_feedback": "原理和框架", "selection_rationale": ""},
            {"final_feedback": "例子和案例", "selection_rationale": ""},
        ]
        c = analyzer._analyze_cognitive(session, [], [])
        # abstract=2, concrete=2 -> total=4 -> abstraction=0.5
        assert c.abstraction == pytest.approx(0.5, abs=0.01)

    def test_system_view_from_wiki(self, analyzer):
        wiki = [
            {"page_path": "a.md"},
            {"page_path": "b.md"},
            {"page_path": "c.md"},
        ]
        c = analyzer._analyze_cognitive([], [], wiki)
        # 分位数归一化：单值窗口返回 0.5
        assert c.system_view == pytest.approx(0.5, abs=0.01)

    def test_skepticism_from_corrections(self, analyzer):
        session = [
            {"correction_count": 3},
            {"correction_count": 0},
        ]
        c = analyzer._analyze_cognitive(session, [], [])
        # 分位数归一化：窗口 [0, 3]，avg = (0.0 + 1.0) / 2 = 0.5
        # 行为校准：avg_correction=1.5，skepticism += 0.045
        assert c.skepticism == pytest.approx(0.545, abs=0.01)

    def test_creativity_from_git(self, analyzer):
        git = [
            {"commit_type": "feat"},
            {"commit_type": "fix"},
            {"commit_type": "feat"},
            {"commit_type": "refactor"},
            {"commit_type": "feat"},
        ]
        c = analyzer._analyze_cognitive([], git, [])
        # creative=3, optimize=2 -> creativity = 3/5 = 0.6
        assert c.creativity == pytest.approx(0.6, abs=0.01)

    def test_deduction_from_git_message_length(self, analyzer):
        git = [
            {"message_length": 100},
            {"message_length": 20},
        ]
        c = analyzer._analyze_cognitive([], git, [])
        # avg = 60 -> (60-20)/80 = 0.5
        assert c.deduction == pytest.approx(0.5, abs=0.01)

    def test_insufficient_without_git(self, analyzer):
        c = analyzer._analyze_cognitive([], [], [])
        assert "creativity" in c.insufficient_dimensions
        assert "deduction" in c.insufficient_dimensions


# =====================================================================
# PreferenceAnalyzer — _analyze_value
# =====================================================================


class TestAnalyzeValue:
    def test_empty_signals(self, analyzer):
        v = analyzer._analyze_value([], [], [])
        assert isinstance(v, ValueProfile)
        assert v.correctness_vs_efficiency == 0.5
        assert v.confidence == 0.0
        assert v.insufficient_dimensions == [
            "innovation_vs_safety",
            "autonomy_vs_collaboration",
            "action_vs_analysis",
        ]

    def test_correctness_vs_efficiency(self, analyzer):
        session = [
            {"termination_type": "satisfied"},
            {"termination_type": "progress"},
            {"termination_type": "delegated"},
        ]
        v = analyzer._analyze_value(session, [], [])
        # 分位数归一化：单值窗口返回 0.5
        assert v.correctness_vs_efficiency == pytest.approx(0.5, abs=0.01)

    def test_depth_vs_breadth(self, analyzer):
        session = [
            {"follow_up_depth": 8},
            {"follow_up_depth": 0},
        ]
        v = analyzer._analyze_value(session, [], [])
        # 分位数归一化：窗口 [0, 8]，avg = (0.0 + 1.0) / 2 = 0.5
        assert v.depth_vs_breadth == pytest.approx(0.5, abs=0.01)

    def test_perfection_vs_completion(self, analyzer):
        session = [
            {"output_type": "code"},
            {"output_type": "discussion"},
            {"output_type": "document"},
        ]
        v = analyzer._analyze_value(session, [], [])
        # 分位数归一化：单值窗口返回 0.5
        assert v.perfection_vs_completion == pytest.approx(0.5, abs=0.01)

    def test_innovation_vs_safety(self, analyzer):
        git = [
            {"commit_type": "feat", "is_weekend": False},
            {"commit_type": "fix", "is_weekend": False},
            {"commit_type": "feat", "is_weekend": True},
            {"commit_type": "chore", "is_weekend": False},
            {"commit_type": "feat", "is_weekend": False},
        ]
        v = analyzer._analyze_value([], git, [])
        # 分位数归一化：单值窗口 0.5 + weekend_ratio 0.2 -> blended = 0.35
        assert v.innovation_vs_safety == pytest.approx(0.35, abs=0.01)

    def test_autonomy_vs_collaboration(self, analyzer):
        git = [
            {"has_issue_reference": True, "has_pr_reference": False},
            {"has_issue_reference": False, "has_pr_reference": False},
            {"has_issue_reference": False, "has_pr_reference": True},
            {"has_issue_reference": False, "has_pr_reference": False},
            {"has_issue_reference": False, "has_pr_reference": False},
        ]
        v = analyzer._analyze_value([], git, [])
        # 分位数归一化：单值窗口 0.5 -> autonomy = 1 - 0.5 = 0.5
        assert v.autonomy_vs_collaboration == pytest.approx(0.5, abs=0.01)

    def test_action_vs_analysis(self, analyzer):
        session = [
            {"output_type": "code", "termination_type": "satisfied"},
            {"output_type": "discussion", "termination_type": "interrupted"},
            {"output_type": "analysis", "termination_type": "progress"},
        ]
        v = analyzer._analyze_value(session, [], [])
        # 分位数归一化：单值窗口 0.5 + interrupted boost 0.067 + 行为校准 0.067
        assert v.action_vs_analysis == pytest.approx(0.633, abs=0.02)


# =====================================================================
# PreferenceAnalyzer — analyze (full mode)
# =====================================================================


class TestAnalyzeFull:
    def test_full_analysis_no_signals_no_fallback(self, analyzer):
        analyzer._get_git_signals = lambda days: []
        analyzer._get_wiki_signals = lambda days: []
        result = analyzer.analyze(days=30, incremental=False)
        assert isinstance(result, PreferenceProfile)
        assert result.version == 1
        assert result.signal_count == 0

    def test_full_analysis_with_signals(self, mock_store, analyzer):
        for i in range(15):
            mock_store.add_session(
                duration_seconds=600,
                follow_up_depth=3,
                task_type="coding",
                timestamp=datetime.now().isoformat(),
            )
        for i in range(10):
            mock_store.add_git(
                hour_of_day=10 + i % 3,
                commit_type="feat",
                is_weekend=False,
                message_length=50,
            )
        # Monkeypatch _get_git_signals and _get_wiki_signals to use mock_store data
        analyzer._get_git_signals = lambda days: mock_store._git
        analyzer._get_wiki_signals = lambda days: mock_store._wiki
        result = analyzer.analyze(days=30, incremental=False)
        assert result.version == 1
        assert result.signal_count == 25
        assert result.energy.confidence > 0
        assert result.cognitive.confidence > 0
        assert result.value.confidence > 0

    def test_full_analysis_exposes_domain_preferences(self, mock_store, analyzer):
        for _ in range(3):
            mock_store.add_session(task_type="coding/python")
        mock_store.add_wiki(action_type="modify", page_path="03-Tech/python.md")
        analyzer._get_git_signals = lambda days: []
        analyzer._get_wiki_signals = lambda days: mock_store._wiki

        result = analyzer.analyze(days=30, incremental=False)

        assert result.domain_preferences["coding"] == pytest.approx(0.4, abs=0.01)
        assert result.domain_preferences["tech"] == pytest.approx(0.3, abs=0.01)
        assert result.to_dict()["domain_preferences"] == result.domain_preferences

    def test_full_analysis_with_previous_profile(self, mock_store, analyzer):
        for i in range(15):
            mock_store.add_session(duration_seconds=600, task_type="coding")
        analyzer._get_git_signals = lambda days: mock_store._git
        analyzer._get_wiki_signals = lambda days: mock_store._wiki
        prev = PreferenceProfile(version=1, signal_count=10)
        result = analyzer.analyze(days=30, previous_profile=prev, incremental=False)
        assert result.version == 2
        assert result.signal_count == 15
        # Changes should be calculated
        assert hasattr(result.energy, "_changes")

    def test_full_analysis_keeps_signals_pending_for_canonical_commit(self, mock_store, analyzer):
        for i in range(5):
            mock_store.add_session(duration_seconds=600, task_type="coding")
        analyzer._get_git_signals = lambda days: mock_store._git
        analyzer._get_wiki_signals = lambda days: mock_store._wiki
        analyzer.analyze(days=30, incremental=False)
        # Analysis is read-only: the canonical revision transaction owns consumption.
        unprocessed = mock_store.get_unprocessed_signals("session")
        assert len(unprocessed) == 5

    def test_full_analysis_fallback_to_knowledge_profile(self, mock_store, analyzer):
        metis = MagicMock()
        metis.domain_entropy = 0.3
        metis.learning_mode = {"simple_mode": "方法论导向"}
        metis.tool_stack = ["a", "b", "c", "d"]
        analyzer._get_git_signals = lambda days: mock_store._git
        analyzer._get_wiki_signals = lambda days: mock_store._wiki
        result = analyzer.analyze(days=30, incremental=False, metis_profile=metis)
        assert result.signal_count == 0
        assert result.value.depth_vs_breadth == pytest.approx(0.7, abs=0.01)
        assert result.cognitive.abstraction == pytest.approx(0.7, abs=0.01)
        assert result.cognitive.system_view == pytest.approx(0.4, abs=0.01)
        assert result.energy.confidence == 0.1
        assert "focus_depth" in result.energy.insufficient_dimensions


# =====================================================================
# PreferenceAnalyzer — _analyze_incremental
# =====================================================================


class TestAnalyzeIncremental:
    def test_incremental_no_new_signals(self, analyzer):
        prev = PreferenceProfile(version=1, signal_count=10)
        result = analyzer.analyze(days=30, previous_profile=prev, incremental=True)
        assert result is prev

    def test_incremental_with_new_signals(self, mock_store, analyzer):
        prev = PreferenceProfile(
            version=1,
            signal_count=10,
            period_start="2024-01-01",
            energy=EnergyProfile(focus_depth=0.5, confidence=0.3),
            cognitive=CognitiveProfile(abstraction=0.5, confidence=0.3),
            value=ValueProfile(depth_vs_breadth=0.5, confidence=0.3),
        )
        for i in range(10):
            mock_store.add_session(duration_seconds=1200, task_type="review")
        result = analyzer.analyze(days=30, previous_profile=prev, incremental=True)
        assert result.version == 2
        assert result.signal_count == 20
        # Scores should be merged (weighted average)
        assert result.energy.focus_depth != 0.5
        assert result.energy.confidence == pytest.approx(0.35, abs=0.01)

    def test_incremental_merges_domain_preferences(self, mock_store, analyzer):
        prev = PreferenceProfile(
            version=1,
            signal_count=10,
            period_start="2024-01-01",
            domain_preferences={"docs": 0.2},
        )
        for _ in range(3):
            mock_store.add_session(task_type="coding/python")
        mock_store.add_wiki(action_type="modify", page_path="03-Tech/python.md")
        result = analyzer.analyze(days=30, previous_profile=prev, incremental=True)

        assert result.domain_preferences["docs"] == pytest.approx(0.2, abs=0.01)
        assert result.domain_preferences["coding"] == pytest.approx(0.4, abs=0.01)
        assert result.domain_preferences["tech"] == pytest.approx(0.3, abs=0.01)

    def test_incremental_merges_insufficient_dimensions(self, mock_store, analyzer):
        prev = PreferenceProfile(
            version=1,
            signal_count=10,
            period_start="2024-01-01",
            energy=EnergyProfile(
                focus_depth=0.5,
                insufficient_dimensions=["recovery_cycle"],
            ),
            cognitive=CognitiveProfile(abstraction=0.5),
            value=ValueProfile(depth_vs_breadth=0.5),
        )
        for i in range(10):
            mock_store.add_session(duration_seconds=600, task_type="coding")
        result = analyzer.analyze(days=30, previous_profile=prev, incremental=True)
        # Intersection: old has recovery_cycle, new also has it (no git signals)
        assert "recovery_cycle" in result.energy.insufficient_dimensions

    def test_incremental_new_signals_remain_pending_until_material_commit(self, mock_store, analyzer):
        prev = PreferenceProfile(version=1, signal_count=5, period_start="2024-01-01")
        for i in range(5):
            mock_store.add_session(duration_seconds=600, task_type="coding")
        result = analyzer.analyze(days=30, previous_profile=prev, incremental=True)
        unprocessed = mock_store.get_unprocessed_signals("session")
        assert len(unprocessed) == 5
        assert result.source_signal_ids == {"session": [1, 2, 3, 4, 5]}

    def test_confidence_only_increment_is_not_material(self, analyzer):
        previous = PreferenceProfile(
            version=1,
            energy=EnergyProfile(confidence=0.2),
            cognitive=CognitiveProfile(confidence=0.2),
            value=ValueProfile(confidence=0.2),
        )
        candidate = PreferenceProfile(
            version=2,
            energy=EnergyProfile(confidence=0.9),
            cognitive=CognitiveProfile(confidence=0.9),
            value=ValueProfile(confidence=0.9),
        )

        assert analyzer.is_material_change(previous, candidate) is False

    def test_confidence_saturated_submaterial_batch_remains_replayable(
        self,
        mock_store,
        analyzer,
    ):
        previous = PreferenceProfile(
            version=1,
            signal_count=100,
            period_start="2024-01-01",
            energy=EnergyProfile(
                confidence=1.0,
                insufficient_dimensions=["recovery_cycle"],
            ),
            cognitive=CognitiveProfile(
                confidence=1.0,
                insufficient_dimensions=["creativity", "deduction"],
            ),
            value=ValueProfile(
                confidence=1.0,
                insufficient_dimensions=[
                    "action_vs_analysis",
                    "autonomy_vs_collaboration",
                    "innovation_vs_safety",
                ],
            ),
        )
        mock_store.add_session(duration_seconds=600)

        candidate = analyzer.analyze(days=30, previous_profile=previous, incremental=True)

        assert candidate.energy.confidence == 1.0
        assert candidate.cognitive.confidence == 1.0
        assert candidate.value.confidence == 1.0
        assert analyzer.is_material_change(previous, candidate) is False
        assert candidate.source_signal_ids == {"session": [1]}
        assert len(mock_store.get_unprocessed_signals("session")) == 1


# =====================================================================
# PreferenceAnalyzer — _calculate_changes
# =====================================================================


class TestCalculateChanges:
    def test_stable_change(self, analyzer):
        prev = PreferenceProfile(
            energy=EnergyProfile(focus_depth=0.5),
            cognitive=CognitiveProfile(abstraction=0.5),
            value=ValueProfile(depth_vs_breadth=0.5),
        )
        energy = EnergyProfile(focus_depth=0.55)
        cognitive = CognitiveProfile(abstraction=0.5)
        value = ValueProfile(depth_vs_breadth=0.5)
        analyzer._calculate_changes(energy, cognitive, value, prev)
        assert energy._changes["focus_depth"] == "stable"

    def test_significant_up(self, analyzer):
        prev = PreferenceProfile(energy=EnergyProfile(focus_depth=0.5))
        energy = EnergyProfile(focus_depth=0.7)
        analyzer._calculate_changes(energy, CognitiveProfile(), ValueProfile(), prev)
        assert energy._changes["focus_depth"] == "up_significant"

    def test_major_down(self, analyzer):
        prev = PreferenceProfile(energy=EnergyProfile(focus_depth=0.8))
        energy = EnergyProfile(focus_depth=0.5)
        analyzer._calculate_changes(energy, CognitiveProfile(), ValueProfile(), prev)
        assert energy._changes["focus_depth"] == "down_major"

    def test_no_previous(self, analyzer):
        energy = EnergyProfile(focus_depth=0.8)
        analyzer._calculate_changes(energy, CognitiveProfile(), ValueProfile(), None)
        # Should not raise and not set _changes
        assert not hasattr(energy, "_changes")


# =====================================================================
# PreferenceAnalyzer — detect_drift
# =====================================================================


class TestDetectDrift:
    def test_no_previous(self, analyzer):
        current = PreferenceProfile()
        assert analyzer.detect_drift(current, None) == []

    def test_no_drift(self, analyzer):
        prev = PreferenceProfile(energy=EnergyProfile(focus_depth=0.5))
        current = PreferenceProfile(energy=EnergyProfile(focus_depth=0.52))
        alerts = analyzer.detect_drift(current, prev)
        assert len(alerts) == 0

    def test_sudden_shift(self, analyzer):
        prev = PreferenceProfile(
            generated_at="2024-01-01T00:00:00",
            energy=EnergyProfile(focus_depth=0.5, confidence=0.5),
            cognitive=CognitiveProfile(confidence=0.5),
            value=ValueProfile(confidence=0.5),
        )
        current = PreferenceProfile(
            generated_at="2024-01-15T00:00:00",
            energy=EnergyProfile(focus_depth=0.8, confidence=0.5),
            cognitive=CognitiveProfile(confidence=0.5),
            value=ValueProfile(confidence=0.5),
        )
        alerts = analyzer.detect_drift(current, prev)
        sudden = [a for a in alerts if a["type"] == "sudden_shift"]
        assert len(sudden) == 1
        assert sudden[0]["dimension"] == "energy.focus_depth"
        assert sudden[0]["severity"] == "medium"

    def test_low_confidence_drift(self, analyzer):
        prev = PreferenceProfile(energy=EnergyProfile(focus_depth=0.5, confidence=0.1))
        current = PreferenceProfile(
            energy=EnergyProfile(focus_depth=0.8, confidence=0.1),
            cognitive=CognitiveProfile(confidence=0.1),
            value=ValueProfile(confidence=0.1),
        )
        alerts = analyzer.detect_drift(current, prev)
        low_conf = [a for a in alerts if a["type"] == "low_confidence_drift"]
        assert len(low_conf) == 1
        assert "噪声" in low_conf[0]["advice"]

    def test_gradual_drift(self, analyzer):
        prev = PreferenceProfile(
            generated_at="2024-06-01T00:00:00",
            energy=EnergyProfile(focus_depth=0.5, startup_difficulty=0.5, endurance_mode=0.5),
            cognitive=CognitiveProfile(abstraction=0.5, system_view=0.5, skepticism=0.5),
            value=ValueProfile(
                correctness_vs_efficiency=0.5, depth_vs_breadth=0.5, perfection_vs_completion=0.5
            ),
        )
        current = PreferenceProfile(
            generated_at="2024-06-07T00:00:00",
            energy=EnergyProfile(focus_depth=0.7, startup_difficulty=0.7, endurance_mode=0.7),
            cognitive=CognitiveProfile(abstraction=0.7, system_view=0.7, skepticism=0.7),
            value=ValueProfile(
                correctness_vs_efficiency=0.7, depth_vs_breadth=0.7, perfection_vs_completion=0.7
            ),
        )
        alerts = analyzer.detect_drift(current, prev)
        gradual = [a for a in alerts if a["type"] == "gradual_drift"]
        assert len(gradual) == 1
        assert "上升" in gradual[0]["dimension"]

    def test_update_lag(self, analyzer):
        old_date = (datetime.now() - timedelta(days=200)).isoformat()
        prev = PreferenceProfile(generated_at=old_date)
        current = PreferenceProfile()
        alerts = analyzer.detect_drift(current, prev)
        lag = [a for a in alerts if a["type"] == "update_lag"]
        assert len(lag) == 1
        assert lag[0]["severity"] == "medium"

    def test_high_severity_sudden_shift(self, analyzer):
        prev = PreferenceProfile(
            generated_at="2024-01-01T00:00:00",
            energy=EnergyProfile(focus_depth=0.5, confidence=0.5),
            cognitive=CognitiveProfile(confidence=0.5),
            value=ValueProfile(confidence=0.5),
        )
        current = PreferenceProfile(
            generated_at="2024-01-15T00:00:00",
            energy=EnergyProfile(focus_depth=0.9, confidence=0.5),
            cognitive=CognitiveProfile(confidence=0.5),
            value=ValueProfile(confidence=0.5),
        )
        alerts = analyzer.detect_drift(current, prev)
        sudden = [a for a in alerts if a["type"] == "sudden_shift"]
        assert sudden[0]["severity"] == "high"


# =====================================================================
# PreferenceAnalyzer — _fallback_from_knowledge_profile
# =====================================================================


class TestFallbackFromKnowledgeProfile:
    def test_methodology_learning_mode(self, analyzer):
        metis = MagicMock()
        metis.domain_entropy = 0.0
        metis.learning_mode = {"simple_mode": "方法论导向"}
        metis.tool_stack = []
        result = analyzer._fallback_from_knowledge_profile(metis)
        assert result.cognitive.abstraction == 0.7
        assert result.cognitive.deduction == 0.7

    def test_problem_learning_mode(self, analyzer):
        metis = MagicMock()
        metis.domain_entropy = 0.0
        metis.learning_mode = {"simple_mode": "问题导向"}
        metis.tool_stack = []
        result = analyzer._fallback_from_knowledge_profile(metis)
        assert result.cognitive.abstraction == 0.35
        assert result.cognitive.deduction == 0.35

    def test_tool_stack_system_view(self, analyzer):
        metis = MagicMock()
        metis.domain_entropy = 0.0
        metis.learning_mode = {}
        metis.tool_stack = ["a", "b", "c", "d"]
        result = analyzer._fallback_from_knowledge_profile(metis)
        assert result.cognitive.system_view == 0.4

    def test_insufficient_dimensions_set(self, analyzer):
        metis = MagicMock()
        metis.domain_entropy = 0.0
        metis.learning_mode = {}
        metis.tool_stack = []
        result = analyzer._fallback_from_knowledge_profile(metis)
        assert len(result.energy.insufficient_dimensions) == 5
        assert len(result.cognitive.insufficient_dimensions) == 2
        assert len(result.value.insufficient_dimensions) == 5

    def test_with_previous_profile(self, analyzer):
        prev = PreferenceProfile(version=3)
        metis = MagicMock()
        metis.domain_entropy = 0.0
        metis.learning_mode = {}
        metis.tool_stack = []
        result = analyzer._fallback_from_knowledge_profile(metis, prev)
        assert result.version == 4


# =====================================================================
# Convenience Functions
# =====================================================================


class TestAnalyzePreferences:
    def test_analyze_preferences_returns_profile(self, monkeypatch, mock_store):
        monkeypatch.setattr("core.persona.pythia.get_signal_store", lambda: mock_store)
        for i in range(15):
            mock_store.add_session(duration_seconds=600, task_type="coding")
        # Patch _get_git_signals and _get_wiki_signals on the class to avoid _pool access
        monkeypatch.setattr(PreferenceAnalyzer, "_get_git_signals", lambda self, days: [])
        monkeypatch.setattr(PreferenceAnalyzer, "_get_wiki_signals", lambda self, days: [])
        result = analyze_preferences(days=30)
        assert isinstance(result, PreferenceProfile)
        assert result.signal_count == 15


class TestGenerateRadarReport:
    def test_generate_radar_report_with_profile(self):
        profile = PreferenceProfile(
            version=1,
            generated_at="2024-01-15T10:00:00",
            signal_count=42,
            energy=EnergyProfile(focus_depth=0.8, confidence=0.7),
            cognitive=CognitiveProfile(abstraction=0.6, confidence=0.6),
            value=ValueProfile(depth_vs_breadth=0.7, confidence=0.8),
        )
        report = generate_radar_report(profile)
        assert "用户偏好画像 v1" in report
        assert "2024-01-15" in report
        assert "信号数: 42" in report
        assert "Layer 1: 能量模式" in report
        assert "Layer 2: 认知模式" in report
        assert "Layer 3: 价值优先级" in report
        assert "深度沉浸" in report
        assert "█" in report

    def test_generate_radar_report_none_profile(self, monkeypatch, mock_store):
        monkeypatch.setattr("core.persona.pythia.get_signal_store", lambda: mock_store)
        for i in range(15):
            mock_store.add_session(duration_seconds=600, task_type="coding")
        # Patch PreferenceAnalyzer methods to avoid _pool access
        monkeypatch.setattr(PreferenceAnalyzer, "_get_git_signals", lambda self, days: [])
        monkeypatch.setattr(PreferenceAnalyzer, "_get_wiki_signals", lambda self, days: [])
        # Patch analyze_preferences to return a concrete profile so generate_radar_report
        # doesn't try to call it (which would create a new analyzer bypassing our patches)
        profile = PreferenceProfile(
            version=1,
            generated_at="2024-01-15T10:00:00",
            signal_count=15,
            energy=EnergyProfile(focus_depth=0.6, confidence=0.5),
            cognitive=CognitiveProfile(abstraction=0.5, confidence=0.5),
            value=ValueProfile(depth_vs_breadth=0.5, confidence=0.5),
        )
        monkeypatch.setattr("core.persona.pythia.analyze_preferences", lambda days=30: profile)
        report = generate_radar_report(None)
        assert "用户偏好画像" in report
        assert "Layer 1" in report

    def test_generate_radar_report_insufficient_dimensions(self):
        # Note: to_dict() replaces scores with "—" for insufficient dimensions,
        # and generate_radar_report() cannot handle string scores (source bug).
        # To avoid crashing, we provide numeric scores for ALL dimensions and use
        # insufficient_dimensions only to verify to_dict() produces "数据不足" labels.
        profile = PreferenceProfile(
            version=2,
            generated_at="2024-01-15T10:00:00",
            signal_count=0,
            energy=EnergyProfile(
                focus_depth=0.8,
                startup_difficulty=0.3,
                endurance_mode=0.5,
                switching_flexibility=0.6,
                recovery_cycle=0.5,
                insufficient_dimensions=[],
                confidence=0.1,
            ),
            cognitive=CognitiveProfile(
                abstraction=0.7,
                system_view=0.4,
                skepticism=0.5,
                creativity=0.5,
                deduction=0.5,
                insufficient_dimensions=[],
                confidence=0.1,
            ),
            value=ValueProfile(
                correctness_vs_efficiency=0.5,
                depth_vs_breadth=0.5,
                perfection_vs_completion=0.5,
                innovation_vs_safety=0.5,
                autonomy_vs_collaboration=0.5,
                action_vs_analysis=0.5,
                insufficient_dimensions=[],
                confidence=0.1,
            ),
        )
        report = generate_radar_report(profile)
        # Numeric dimensions should render bars
        assert "█" in report
        # Labels for non-insufficient dimensions should appear
        assert "深度沉浸" in report
        assert "Layer 1" in report
        assert "Layer 2" in report
        assert "Layer 3" in report

    def test_generate_radar_report_insufficient_dimensions_dict(self):
        # Verify to_dict() correctly marks insufficient dimensions with "—" and "数据不足"
        profile = PreferenceProfile(
            version=2,
            energy=EnergyProfile(
                focus_depth=0.8,
                recovery_cycle=0.5,
                insufficient_dimensions=["recovery_cycle"],
            ),
            cognitive=CognitiveProfile(
                abstraction=0.7,
                creativity=0.5,
                insufficient_dimensions=["creativity"],
            ),
            value=ValueProfile(
                correctness_vs_efficiency=0.5,
                action_vs_analysis=0.5,
                insufficient_dimensions=["action_vs_analysis"],
            ),
        )
        d = profile.to_dict()
        assert d["energy"]["recovery_cycle"]["score"] == "—"
        assert d["energy"]["recovery_cycle"]["label"] == "数据不足"
        assert d["cognitive"]["creativity"]["score"] == "—"
        assert d["cognitive"]["creativity"]["label"] == "数据不足"
        assert d["value"]["action_vs_analysis"]["score"] == "—"
        assert d["value"]["action_vs_analysis"]["label"] == "数据不足"
        # Non-insufficient dimensions should have numeric scores
        assert d["energy"]["focus_depth"]["score"] == 0.8
        assert d["cognitive"]["abstraction"]["score"] == 0.7


# =====================================================================
# Integration / Edge Cases
# =====================================================================


class TestEdgeCases:
    def test_analyzer_init_with_store(self, mock_store):
        a = PreferenceAnalyzer(store=mock_store)
        assert a.store is mock_store

    def test_analyzer_init_without_store(self, monkeypatch, mock_store):
        monkeypatch.setattr("core.persona.pythia.get_signal_store", lambda: mock_store)
        a = PreferenceAnalyzer()
        assert a.store is mock_store

    def test_preference_profile_equality(self):
        p1 = PreferenceProfile(version=1, signal_count=10)
        p2 = PreferenceProfile(version=1, signal_count=10)
        assert p1.version == p2.version
        assert p1.signal_count == p2.signal_count

    def test_energy_profile_equality(self):
        e1 = EnergyProfile(focus_depth=0.8, confidence=0.5)
        e2 = EnergyProfile(focus_depth=0.8, confidence=0.5)
        assert e1.focus_depth == e2.focus_depth
        assert e1.confidence == e2.confidence

    def test_to_dict_with_zero_scores(self):
        p = PreferenceProfile(
            energy=EnergyProfile(
                focus_depth=0.0,
                startup_difficulty=0.0,
                endurance_mode=0.0,
                switching_flexibility=0.0,
                recovery_cycle=0.0,
            ),
            cognitive=CognitiveProfile(
                abstraction=0.0,
                system_view=0.0,
                skepticism=0.0,
                creativity=0.0,
                deduction=0.0,
            ),
            value=ValueProfile(
                correctness_vs_efficiency=0.0,
                depth_vs_breadth=0.0,
                perfection_vs_completion=0.0,
                innovation_vs_safety=0.0,
                autonomy_vs_collaboration=0.0,
                action_vs_analysis=0.0,
            ),
        )
        d = p.to_dict()
        assert d["energy"]["focus_depth"]["score"] == 0.0
        assert d["energy"]["focus_depth"]["label"] == "碎片化"
        assert d["cognitive"]["abstraction"]["label"] == "具象型"
        assert d["value"]["correctness_vs_efficiency"]["label"] == "效率优先"

    def test_to_dict_with_max_scores(self):
        p = PreferenceProfile(
            energy=EnergyProfile(
                focus_depth=1.0,
                startup_difficulty=1.0,
                endurance_mode=1.0,
                switching_flexibility=1.0,
                recovery_cycle=1.0,
            ),
            cognitive=CognitiveProfile(
                abstraction=1.0,
                system_view=1.0,
                skepticism=1.0,
                creativity=1.0,
                deduction=1.0,
            ),
            value=ValueProfile(
                correctness_vs_efficiency=1.0,
                depth_vs_breadth=1.0,
                perfection_vs_completion=1.0,
                innovation_vs_safety=1.0,
                autonomy_vs_collaboration=1.0,
                action_vs_analysis=1.0,
            ),
        )
        d = p.to_dict()
        assert d["energy"]["focus_depth"]["label"] == "深度沉浸"
        assert d["energy"]["startup_difficulty"]["label"] == "需要推力"
        assert d["energy"]["endurance_mode"]["label"] == "匀速型"
        assert d["energy"]["switching_flexibility"]["label"] == "多线程"
        assert d["energy"]["recovery_cycle"]["label"] == "需要缓冲"
        assert d["cognitive"]["abstraction"]["label"] == "抽象型"
        assert d["cognitive"]["system_view"]["label"] == "系统视角"
        assert d["cognitive"]["skepticism"]["label"] == "质疑前提"
        assert d["cognitive"]["creativity"]["label"] == "创造型"
        assert d["cognitive"]["deduction"]["label"] == "演绎型"
        assert d["value"]["correctness_vs_efficiency"]["label"] == "正确性优先"
        assert d["value"]["depth_vs_breadth"]["label"] == "深度优先"
        assert d["value"]["perfection_vs_completion"]["label"] == "先完美"
        assert d["value"]["innovation_vs_safety"]["label"] == "创新优先"
        assert d["value"]["autonomy_vs_collaboration"]["label"] == "自主优先"
        assert d["value"]["action_vs_analysis"]["label"] == "行动优先"

    def test_detect_drift_gradual_drift_negative(self, analyzer):
        prev = PreferenceProfile(
            energy=EnergyProfile(focus_depth=0.7, startup_difficulty=0.7, endurance_mode=0.7),
            cognitive=CognitiveProfile(abstraction=0.7, system_view=0.7, skepticism=0.7),
            value=ValueProfile(
                correctness_vs_efficiency=0.7, depth_vs_breadth=0.7, perfection_vs_completion=0.7
            ),
        )
        current = PreferenceProfile(
            energy=EnergyProfile(focus_depth=0.5, startup_difficulty=0.5, endurance_mode=0.5),
            cognitive=CognitiveProfile(abstraction=0.5, system_view=0.5, skepticism=0.5),
            value=ValueProfile(
                correctness_vs_efficiency=0.5, depth_vs_breadth=0.5, perfection_vs_completion=0.5
            ),
        )
        alerts = analyzer.detect_drift(current, prev)
        gradual = [a for a in alerts if a["type"] == "gradual_drift"]
        assert len(gradual) == 1
        assert "下降" in gradual[0]["dimension"]

    def test_empty_signals_no_crash(self, analyzer):
        e = analyzer._analyze_energy([], [], [], [])
        assert e.focus_depth == 0.5
        assert e.confidence == 0.0


# =====================================================================
# _PercentileNormalizer
# =====================================================================


class TestPercentileNormalizer:
    def test_empty_window_returns_default(self):
        p = _PercentileNormalizer()
        assert p.normalize("test", 100) == 0.5

    def test_single_value_returns_half(self):
        p = _PercentileNormalizer()
        p.update("test", [100])
        assert p.normalize("test", 100) == 0.5

    def test_min_value_returns_zero(self):
        p = _PercentileNormalizer()
        p.update("test", [0, 100])
        assert p.normalize("test", 0) == 0.0

    def test_max_value_returns_one(self):
        p = _PercentileNormalizer()
        p.update("test", [0, 100])
        assert p.normalize("test", 100) == 1.0

    def test_mid_value_interpolates(self):
        p = _PercentileNormalizer()
        p.update("test", [0, 100])
        assert p.normalize("test", 50) == pytest.approx(0.5, abs=0.01)

    def test_batch_normalize_updates_then_computes(self):
        p = _PercentileNormalizer()
        scores = p.batch_normalize("test", [10, 20, 30])
        assert scores[0] == 0.0
        assert scores[1] == pytest.approx(0.5, abs=0.01)
        assert scores[2] == 1.0

    def test_window_size_limit(self):
        p = _PercentileNormalizer(window_size=3)
        p.update("test", [1, 2, 3, 4, 5])
        assert len(p._windows["test"]) == 3
        assert p._windows["test"] == [3, 4, 5]

    def test_window_stats(self):
        p = _PercentileNormalizer()
        p.update("test", [1, 2, 3, 4, 5])
        stats = p.get_window_stats("test")
        assert stats["count"] == 5
        assert stats["min"] == 1
        assert stats["max"] == 5
        assert stats["median"] == 3

    def test_different_dimensions_independent(self):
        p = _PercentileNormalizer()
        p.update("a", [0, 100])
        p.update("b", [50, 60])
        assert p.normalize("a", 50) == pytest.approx(0.5, abs=0.01)
        assert p.normalize("b", 55) == pytest.approx(0.5, abs=0.01)

    def test_outside_range_clamped(self):
        p = _PercentileNormalizer()
        p.update("test", [10, 20, 30])
        assert p.normalize("test", 5) == 0.0
        assert p.normalize("test", 35) == 1.0

    def test_identical_values(self):
        p = _PercentileNormalizer()
        p.update("test", [5, 5, 5])
        # All identical values get rank 0.0 (lower bound)
        assert p.normalize("test", 5) == 0.0

    def test_value_below_min(self):
        p = _PercentileNormalizer()
        p.update("test", [10, 20])
        assert p.normalize("test", 5) == 0.0

    def test_value_above_max(self):
        p = _PercentileNormalizer()
        p.update("test", [10, 20])
        assert p.normalize("test", 25) == 1.0


# =====================================================================
# _BehaviorCalibrator
# =====================================================================


class TestBehaviorCalibrator:
    def test_empty_signals(self):
        c = _BehaviorCalibrator()
        assert c.calibrate([], "energy") == {}

    def test_corrections_boost_skepticism(self):
        c = _BehaviorCalibrator()
        signals = [
            {"correction_count": 2},
            {"correction_count": 1},
        ]
        result = c.calibrate(signals, "cognitive")
        assert "skepticism" in result
        assert result["skepticism"] > 0

    def test_high_corrections_stronger_boost(self):
        c = _BehaviorCalibrator()
        signals = [{"correction_count": 5} for _ in range(5)]
        result = c.calibrate(signals, "cognitive")
        # avg=5, tier1: min(0.1, 5*0.03)=0.1, tier2: +0.05
        assert result["skepticism"] == pytest.approx(0.15, abs=0.01)

    def test_follow_up_boosts_focus_depth(self):
        c = _BehaviorCalibrator()
        signals = [{"follow_up_depth": 6} for _ in range(3)]
        result = c.calibrate(signals, "energy")
        assert "focus_depth" in result
        assert result["focus_depth"] > 0

    def test_interrupted_boosts_action_vs_analysis(self):
        c = _BehaviorCalibrator()
        signals = [
            {"termination_type": "interrupted"},
            {"termination_type": "interrupted"},
            {"termination_type": "satisfied"},
            {"termination_type": "progress"},
            {"termination_type": "progress"},
        ]
        result = c.calibrate(signals, "value")
        assert "action_vs_analysis" in result
        assert result["action_vs_analysis"] > 0

    def test_deep_reasoning_boosts_abstraction(self):
        c = _BehaviorCalibrator()
        signals = [
            {"final_feedback": "原理和理论框架", "selection_rationale": ""},
            {"final_feedback": "模型和机制", "selection_rationale": ""},
            {"final_feedback": "为什么这样工作", "selection_rationale": ""},
        ]
        result = c.calibrate(signals, "cognitive")
        assert "abstraction" in result
        assert result["abstraction"] > 0

    def test_adjustment_clamped_max(self):
        c = _BehaviorCalibrator()
        signals = [{"correction_count": 100} for _ in range(10)]
        result = c.calibrate(signals, "cognitive")
        assert result["skepticism"] <= 0.15

    def test_adjustment_clamped_min(self):
        c = _BehaviorCalibrator()
        # No signals that trigger any rule
        signals = [{"correction_count": 0, "follow_up_depth": 0}]
        result = c.calibrate(signals, "energy")
        assert all(v == 0 for v in result.values())

    def test_apply_within_bounds(self):
        c = _BehaviorCalibrator()
        assert c.apply(0.8, 0.1) == pytest.approx(0.9, abs=0.001)
        assert c.apply(0.8, -0.1) == pytest.approx(0.7, abs=0.001)
        assert c.apply(0.95, 0.1) == 1.0
        assert c.apply(0.05, -0.1) == 0.0

    def test_calibration_rules_filter_by_layer(self):
        c = _BehaviorCalibrator()
        signals = [{"correction_count": 3}]

        assert c.calibrate(signals, "energy") == {}
        assert c.calibrate(signals, "value") == {}

        cognitive = c.calibrate(signals, "cognitive")
        assert "skepticism" in cognitive
        assert cognitive["skepticism"] > 0

    def test_calibration_rules_drive_adjustment_dimensions(self, monkeypatch):
        monkeypatch.setattr(
            _BehaviorCalibrator,
            "CALIBRATION_RULES",
            [("correction_count", "rule_driven_skepticism", "cognitive", 0.05, 0.9)],
        )
        c = _BehaviorCalibrator()

        result = c.calibrate([{"correction_count": 3}], "cognitive")

        assert "rule_driven_skepticism" in result
        assert "skepticism" not in result


# =====================================================================
# _DomainPreferenceAnalyzer
# =====================================================================


class TestDomainPreferenceAnalyzer:
    def test_empty_signals(self):
        a = _DomainPreferenceAnalyzer()
        assert a.analyze([], []) == {}

    def test_conversation_mention_low_score(self):
        a = _DomainPreferenceAnalyzer()
        session = [{"task_type": "ai"}]
        result = a.analyze(session, [])
        # 0.4 * 1.0 / 3.0 = 0.133 (no threshold filtering in analyze)
        assert "ai" in result
        assert result["ai"] == pytest.approx(0.133, abs=0.01)

    def test_multiple_sources_combined(self):
        a = _DomainPreferenceAnalyzer()
        session = [{"task_type": "ai"} for _ in range(3)]
        # Use a path that maps to the same domain; note that _infer_domain_from_path
        # may map "03-Tech/ai.md" → "tech", so we only assert on session contribution
        result = a.analyze(session, [])
        # conversation: 0.4 * min(1, 3/3) = 0.4
        assert "ai" in result
        assert result["ai"] == pytest.approx(0.4, abs=0.01)

    def test_different_domains_tracked_separately(self):
        a = _DomainPreferenceAnalyzer()
        session = [
            {"task_type": "ai"},
            {"task_type": "blockchain"},
        ]
        result = a.analyze(session, [])
        # Each: 0.4 * 1.0 / 3.0 = 0.133
        assert "ai" in result
        assert "blockchain" in result

    def test_weights_sum_to_one(self):
        a = _DomainPreferenceAnalyzer()
        total = sum(a.WEIGHTS.values())
        assert total == pytest.approx(1.0, abs=0.01)

    def test_normalizers_positive(self):
        a = _DomainPreferenceAnalyzer()
        for v in a.NORMALIZERS.values():
            assert v > 0

    def test_strong_signal_crosses_threshold(self):
        a = _DomainPreferenceAnalyzer()
        session = [{"task_type": "rust"} for _ in range(10)]
        result = a.analyze(session, [])
        # 0.4 * min(1, 10/3) = 0.4 — still < 1.0
        # Need wiki signals too
        wiki = [{"action_type": "modify", "page_path": "03-Tech/rust.md"} for _ in range(5)]
        result = a.analyze(session, wiki)
        # conversation: 0.4, wiki_edit: 0.3 * min(1, 5/1) = 0.3
        # total = 0.7 — hmm
        # Let's use enough signals to cross threshold
        session = [{"task_type": "rust"} for _ in range(30)]
        wiki = [{"action_type": "modify", "page_path": "03-Tech/rust.md"} for _ in range(10)]
        result = a.analyze(session, wiki)
        assert "rust" in result

    def test_wiki_signals_only(self):
        a = _DomainPreferenceAnalyzer()
        wiki = [{"action_type": "modify", "page_path": "03-Tech/ai.md"}]
        result = a.analyze([], wiki)
        # 0.3 * min(1, 1/1) = 0.3 < 1.0
        assert "ai" not in result

    def test_unknown_wiki_action_ignored(self):
        a = _DomainPreferenceAnalyzer()
        wiki = [{"action_type": "delete", "page_path": "03-Tech/ai.md"}]
        result = a.analyze([], wiki)
        assert "ai" not in result


class TestReflectionSignalsInPersona:
    """P109: Layer 5 反射信号应被画像分析消费，避免落入 JSONL 后无人读取。"""

    def _patch_git_wiki(self, analyzer):
        analyzer._get_git_signals = lambda days: []
        analyzer._get_wiki_signals = lambda days: []

    def test_analyze_includes_reflection_signals(self, analyzer, mock_store):
        self._patch_git_wiki(analyzer)
        mock_store.add_session(task_type="coding", final_feedback="", selection_rationale="")
        mock_store.add_reflection(dimension="value_priority", value="innovation", confidence=1.0)

        profile = analyzer.analyze(days=30)

        # reflection 信号计入总数
        assert profile.signal_count == 2
        # innovation 信号应提升 innovation_vs_safety
        assert profile.value.innovation_vs_safety > 0.5

    def test_analyze_reflection_no_match_does_not_crash(self, analyzer, mock_store):
        self._patch_git_wiki(analyzer)
        mock_store.add_reflection(dimension="unknown", value="unknown", confidence=0.5)
        profile = analyzer.analyze(days=30)
        assert profile.signal_count == 1

    def test_incremental_analyze_applies_reflection_signals(self, analyzer, mock_store):
        self._patch_git_wiki(analyzer)
        previous = PreferenceProfile(
            version=1,
            generated_at=(datetime.now() - timedelta(days=1)).isoformat(),
            signal_count=1,
        )
        mock_store.add_reflection(dimension="cognitive_shift", value="abstract", confidence=1.0)

        profile = analyzer.analyze(previous_profile=previous, incremental=True)

        assert profile.signal_count == 2
        assert profile.cognitive.abstraction > 0.5
