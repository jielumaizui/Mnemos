"""
Preference Analyzer - 偏好画像分析器（三层雷达）

职责：
- 从聚合信号中推断用户的偏好画像
- 生成三层雷达图：能量模式 / 认知模式 / 价值优先级
- 计算每个维度的置信度和变化趋势

分析原则：
- 不是统计「用户做了什么」，而是推断「用户是什么样的人」
- 每个维度必须有足够信号支撑，否则标记为「insufficient_data」
- 变化趋势通过与上一周期画像对比得出
"""
# Pythia — 德尔斐女祭司 — 偏好分析，解读用户行为信号
# 原模块: preference_analyzer.py



import json
import math
from typing import Dict, List, Optional, Any, Tuple
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from collections import Counter, defaultdict

from .psyche import SignalStore, get_signal_store
import logging

logger = logging.getLogger(__name__)


# ========== 三层雷达模型定义 ==========

@dataclass
class EnergyProfile:
    """Layer 1: 能量模式 - 你的能量怎么流动"""
    focus_depth: float = 0.5           # 专注深度 0=碎片化, 1=深度沉浸
    startup_difficulty: float = 0.5    # 启动难度 0=一触即发, 1=需要推力
    endurance_mode: float = 0.5        # 续航模式 0=爆发型, 1=匀速型
    switching_flexibility: float = 0.5 # 切换弹性 0=单线程, 1=多线程
    recovery_cycle: float = 0.5        # 恢复周期 0=快速恢复, 1=需要缓冲
    confidence: float = 0.0
    insufficient_dimensions: List[str] = None  # 数据不足的维度列表


@dataclass
class CognitiveProfile:
    """Layer 2: 认知模式 - 你的大脑默认怎么运转"""
    abstraction: float = 0.5           # 抽象↔具象 0=从案例归纳, 1=从原理推导
    system_view: float = 0.5           # 系统↔单点 0=聚焦当前, 1=先看全局
    skepticism: float = 0.5            # 质疑↔信任 0=信任框架, 1=挑战前提
    creativity: float = 0.5            # 创造↔优化 0=从1到N, 1=从0到1
    deduction: float = 0.5             # 演绎↔归纳 0=从经验总结, 1=从规则推导
    confidence: float = 0.0
    insufficient_dimensions: List[str] = None


@dataclass
class ValueProfile:
    """Layer 3: 价值优先级 - 你做选择时的底层权重"""
    correctness_vs_efficiency: float = 0.5   # 正确性↔效率
    depth_vs_breadth: float = 0.5            # 深度↔广度
    perfection_vs_completion: float = 0.5    # 完美↔完成
    innovation_vs_safety: float = 0.5        # 创新↔稳妥
    autonomy_vs_collaboration: float = 0.5   # 自主↔协作
    confidence: float = 0.0
    insufficient_dimensions: List[str] = None


@dataclass
class PreferenceProfile:
    """完整偏好画像"""
    version: int = 0
    generated_at: str = ""
    period_start: str = ""
    period_end: str = ""
    energy: EnergyProfile = field(default_factory=EnergyProfile)
    cognitive: CognitiveProfile = field(default_factory=CognitiveProfile)
    value: ValueProfile = field(default_factory=ValueProfile)
    signal_count: int = 0

    def to_dict(self) -> Dict:
        return {
            "version": self.version,
            "generated_at": self.generated_at,
            "period_start": self.period_start,
            "period_end": self.period_end,
            "energy": self._energy_to_dict(),
            "cognitive": self._cognitive_to_dict(),
            "value": self._value_to_dict(),
            "signal_count": self.signal_count,
        }

    def _energy_to_dict(self) -> Dict:
        ins = set(self.energy.insufficient_dimensions or [])
        return {
            "focus_depth": {"score": round(self.energy.focus_depth, 2), "label": self._label_depth(self.energy.focus_depth)},
            "startup_difficulty": {"score": round(self.energy.startup_difficulty, 2), "label": self._label_startup(self.energy.startup_difficulty)},
            "endurance_mode": {"score": round(self.energy.endurance_mode, 2), "label": "爆发型" if self.energy.endurance_mode < 0.4 else "匀速型" if self.energy.endurance_mode > 0.6 else "混合型"},
            "switching_flexibility": {"score": round(self.energy.switching_flexibility, 2), "label": "单线程" if self.energy.switching_flexibility < 0.4 else "多线程" if self.energy.switching_flexibility > 0.6 else "弹性切换"},
            "recovery_cycle": {"score": "—", "label": "数据不足"} if "recovery_cycle" in ins else {"score": round(self.energy.recovery_cycle, 2), "label": "快速恢复" if self.energy.recovery_cycle < 0.4 else "需要缓冲" if self.energy.recovery_cycle > 0.6 else "中等恢复"},
            "confidence": round(self.energy.confidence, 2),
        }

    def _cognitive_to_dict(self) -> Dict:
        ins = set(self.cognitive.insufficient_dimensions or [])
        return {
            "abstraction": {"score": round(self.cognitive.abstraction, 2), "label": "具象型" if self.cognitive.abstraction < 0.4 else "抽象型" if self.cognitive.abstraction > 0.6 else "平衡型"},
            "system_view": {"score": round(self.cognitive.system_view, 2), "label": "单点聚焦" if self.cognitive.system_view < 0.4 else "系统视角" if self.cognitive.system_view > 0.6 else "视情况"},
            "skepticism": {"score": round(self.cognitive.skepticism, 2), "label": "信任框架" if self.cognitive.skepticism < 0.4 else "质疑前提" if self.cognitive.skepticism > 0.6 else "适度质疑"},
            "creativity": {"score": "—", "label": "数据不足"} if "creativity" in ins else {"score": round(self.cognitive.creativity, 2), "label": "优化型" if self.cognitive.creativity < 0.4 else "创造型" if self.cognitive.creativity > 0.6 else "两者兼顾"},
            "deduction": {"score": "—", "label": "数据不足"} if "deduction" in ins else {"score": round(self.cognitive.deduction, 2), "label": "归纳型" if self.cognitive.deduction < 0.4 else "演绎型" if self.cognitive.deduction > 0.6 else "混合使用"},
            "confidence": round(self.cognitive.confidence, 2),
        }

    def _value_to_dict(self) -> Dict:
        ins = set(self.value.insufficient_dimensions or [])
        return {
            "correctness_vs_efficiency": {"score": round(self.value.correctness_vs_efficiency, 2), "label": "效率优先" if self.value.correctness_vs_efficiency < 0.4 else "正确性优先" if self.value.correctness_vs_efficiency > 0.6 else "视情况平衡"},
            "depth_vs_breadth": {"score": round(self.value.depth_vs_breadth, 2), "label": "广度优先" if self.value.depth_vs_breadth < 0.4 else "深度优先" if self.value.depth_vs_breadth > 0.6 else "两者兼顾"},
            "perfection_vs_completion": {"score": round(self.value.perfection_vs_completion, 2), "label": "先完成" if self.value.perfection_vs_completion < 0.4 else "先完美" if self.value.perfection_vs_completion > 0.6 else "平衡"},
            "innovation_vs_safety": {"score": "—", "label": "数据不足"} if "innovation_vs_safety" in ins else {"score": round(self.value.innovation_vs_safety, 2), "label": "稳妥优先" if self.value.innovation_vs_safety < 0.4 else "创新优先" if self.value.innovation_vs_safety > 0.6 else "视风险而定"},
            "autonomy_vs_collaboration": {"score": "—", "label": "数据不足"} if "autonomy_vs_collaboration" in ins else {"score": round(self.value.autonomy_vs_collaboration, 2), "label": "协作优先" if self.value.autonomy_vs_collaboration < 0.4 else "自主优先" if self.value.autonomy_vs_collaboration > 0.6 else "灵活切换"},
            "confidence": round(self.value.confidence, 2),
        }

    @staticmethod
    def _label_depth(score: float) -> str:
        if score < 0.3: return "碎片化"
        if score < 0.5: return "中等专注"
        if score < 0.7: return "较深度"
        return "深度沉浸"

    @staticmethod
    def _label_startup(score: float) -> str:
        if score < 0.3: return "一触即发"
        if score < 0.5: return "启动较快"
        if score < 0.7: return "需要准备"
        return "需要推力"


# ========== 分析引擎 ==========

class PreferenceAnalyzer:
    """偏好画像分析引擎"""

    # 最小信号数阈值
    MIN_SIGNALS = {
        "session": 10,
        "git": 5,
        "wiki": 5,
        "wechat": 20,
        "file_system": 10,
        "memos": 30,
    }

    def __init__(self, store: SignalStore = None):
        self.store = store or get_signal_store()

    def analyze(self, days: int = 90, previous_profile: PreferenceProfile = None,
                incremental: bool = False) -> PreferenceProfile:
        """
        分析信号，生成偏好画像。

        Args:
            days: 分析时间窗口（全量模式有效）
            previous_profile: 上一周期画像（增量模式必须提供）
            incremental: True=只处理未标记信号并合并，False=全量重新计算

        Returns:
            PreferenceProfile
        """
        if incremental and previous_profile:
            return self._analyze_incremental(previous_profile)

        # 全量模式：读取所有信号
        session_signals = self.store.get_recent_session_signals(days=days)
        git_signals = self._get_git_signals(days=days)
        wiki_signals = self._get_wiki_signals(days=days)
        wechat_signals = self.store.get_recent_wechat_signals(days=days)
        fs_signals = self._get_fs_signals(days=days)
        memos_signals = self.store.get_recent_memos_signals(days=days)

        total_signals = len(session_signals) + len(git_signals) + len(wiki_signals) + len(wechat_signals) + len(fs_signals) + len(memos_signals)

        # 标记所有信号为已处理（增量模式依赖这个标记）
        self._mark_all_processed(session_signals, git_signals, wiki_signals, wechat_signals, fs_signals, memos_signals)

        # 三层雷达分析
        energy = self._analyze_energy(session_signals, git_signals, wechat_signals, fs_signals, memos_signals)
        cognitive = self._analyze_cognitive(session_signals, git_signals, wiki_signals, memos_signals)
        value = self._analyze_value(session_signals, git_signals, wiki_signals, memos_signals)

        if previous_profile:
            self._calculate_changes(energy, cognitive, value, previous_profile)

        return PreferenceProfile(
            version=(previous_profile.version + 1) if previous_profile else 1,
            generated_at=datetime.now().isoformat(),
            period_start=(datetime.now() - timedelta(days=days)).isoformat()[:10],
            period_end=datetime.now().isoformat()[:10],
            energy=energy,
            cognitive=cognitive,
            value=value,
            signal_count=total_signals,
        )

    def _analyze_incremental(self, previous: PreferenceProfile) -> PreferenceProfile:
        """
        增量分析：只处理未标记信号，与上一画像合并。
        使用加权平均更新各维度分数。
        """
        # 获取未处理信号
        new_session = self.store.get_unprocessed_signals("session", limit=1000)
        new_git = self.store.get_unprocessed_signals("git", limit=1000)
        new_wiki = self.store.get_unprocessed_signals("knowledge", limit=1000)
        new_wechat = self.store.get_unprocessed_signals("wechat", limit=1000)
        new_fs = self.store.get_unprocessed_signals("file_system", limit=1000)
        new_memos = self.store.get_unprocessed_signals("memos", limit=5000)

        total_new = len(new_session) + len(new_git) + len(new_wiki) + len(new_wechat) + len(new_fs) + len(new_memos)
        if total_new == 0:
            return previous

        # 基于新信号计算增量分数
        delta_energy = self._analyze_energy(new_session, new_git, new_wechat, new_fs, new_memos)
        delta_cognitive = self._analyze_cognitive(new_session, new_git, new_wiki, new_memos)
        delta_value = self._analyze_value(new_session, new_git, new_wiki, new_memos)

        # 计算各源权重（用于加权平均）
        old_weight = max(1, previous.signal_count)
        new_weight = max(1, total_new)
        total_weight = old_weight + new_weight

        def merge_score(old: float, new: float) -> float:
            return (old * old_weight + new * new_weight) / total_weight

        # 合并 insufficient_dimensions：新数据仍不足或旧数据不足的都保留
        prev_ins_energy = set(previous.energy.insufficient_dimensions or [])
        delta_ins_energy = set(delta_energy.insufficient_dimensions or [])
        merged_ins_energy = list(prev_ins_energy & delta_ins_energy)  # 交集：两边都不足

        prev_ins_cognitive = set(previous.cognitive.insufficient_dimensions or [])
        delta_ins_cognitive = set(delta_cognitive.insufficient_dimensions or [])
        merged_ins_cognitive = list(prev_ins_cognitive & delta_ins_cognitive)

        prev_ins_value = set(previous.value.insufficient_dimensions or [])
        delta_ins_value = set(delta_value.insufficient_dimensions or [])
        merged_ins_value = list(prev_ins_value & delta_ins_value)

        # 合并能量层
        energy = EnergyProfile(
            focus_depth=merge_score(previous.energy.focus_depth, delta_energy.focus_depth),
            startup_difficulty=merge_score(previous.energy.startup_difficulty, delta_energy.startup_difficulty),
            endurance_mode=merge_score(previous.energy.endurance_mode, delta_energy.endurance_mode),
            switching_flexibility=merge_score(previous.energy.switching_flexibility, delta_energy.switching_flexibility),
            recovery_cycle=merge_score(previous.energy.recovery_cycle, delta_energy.recovery_cycle),
            confidence=min(1.0, previous.energy.confidence + 0.05),
            insufficient_dimensions=merged_ins_energy,
        )

        # 合并认知层
        cognitive = CognitiveProfile(
            abstraction=merge_score(previous.cognitive.abstraction, delta_cognitive.abstraction),
            system_view=merge_score(previous.cognitive.system_view, delta_cognitive.system_view),
            skepticism=merge_score(previous.cognitive.skepticism, delta_cognitive.skepticism),
            creativity=merge_score(previous.cognitive.creativity, delta_cognitive.creativity),
            deduction=merge_score(previous.cognitive.deduction, delta_cognitive.deduction),
            confidence=min(1.0, previous.cognitive.confidence + 0.05),
            insufficient_dimensions=merged_ins_cognitive,
        )

        # 合并价值层
        value = ValueProfile(
            correctness_vs_efficiency=merge_score(previous.value.correctness_vs_efficiency, delta_value.correctness_vs_efficiency),
            depth_vs_breadth=merge_score(previous.value.depth_vs_breadth, delta_value.depth_vs_breadth),
            perfection_vs_completion=merge_score(previous.value.perfection_vs_completion, delta_value.perfection_vs_completion),
            innovation_vs_safety=merge_score(previous.value.innovation_vs_safety, delta_value.innovation_vs_safety),
            autonomy_vs_collaboration=merge_score(previous.value.autonomy_vs_collaboration, delta_value.autonomy_vs_collaboration),
            confidence=min(1.0, previous.value.confidence + 0.05),
            insufficient_dimensions=merged_ins_value,
        )

        # 标记新信号为已处理
        self._mark_all_processed(new_session, new_git, new_wiki, new_wechat, new_fs, new_memos)

        # 计算变化
        self._calculate_changes(energy, cognitive, value, previous)

        return PreferenceProfile(
            version=previous.version + 1,
            generated_at=datetime.now().isoformat(),
            period_start=previous.period_start,
            period_end=datetime.now().isoformat()[:10],
            energy=energy,
            cognitive=cognitive,
            value=value,
            signal_count=previous.signal_count + total_new,
        )

    def _mark_all_processed(self, session, git, wiki, wechat, fs, memos=None):
        """标记信号为已处理"""
        try:
            if session:
                ids = [s["id"] for s in session if "id" in s]
                if ids:
                    self.store.mark_signals_processed("session", ids)
            if git:
                ids = [s["id"] for s in git if "id" in s]
                if ids:
                    self.store.mark_signals_processed("git", ids)
            if wiki:
                ids = [s["id"] for s in wiki if "id" in s]
                if ids:
                    self.store.mark_signals_processed("knowledge", ids)
            if wechat:
                ids = [s["id"] for s in wechat if "id" in s]
                if ids:
                    self.store.mark_signals_processed("wechat", ids)
            if fs:
                ids = [s["id"] for s in fs if "id" in s]
                if ids:
                    self.store.mark_signals_processed("file_system", ids)
            if memos:
                ids = [s["id"] for s in memos if "id" in s]
                if ids:
                    self.store.mark_signals_processed("memos", ids)
        except Exception as e:
            logger.warning(f"忽略异常: {e}")

    # ---- Layer 1: 能量模式分析 ----

    def _analyze_energy(self, session_signals, git_signals, wechat_signals, fs_signals, memos_signals=None) -> EnergyProfile:
        """分析能量模式"""
        profile = EnergyProfile()
        insufficient = []

        # 数据健康度检查：关键数据源必须达到最低要求，否则标记对应维度为 insufficient
        git_healthy = len(git_signals) >= self.MIN_SIGNALS.get("git", 5)
        session_healthy = len(session_signals) >= self.MIN_SIGNALS.get("session", 10)

        # 专注深度：session平均持续时间、消息连贯性
        if session_signals:
            durations = [s.get("duration_seconds", 0) for s in session_signals if s.get("duration_seconds", 0) > 0]
            if durations:
                avg_duration = sum(durations) / len(durations)
                # >30分钟=深度，<5分钟=碎片化
                profile.focus_depth = min(1.0, max(0.0, (avg_duration - 300) / 1500))

            # 追问深度作为辅助信号
            follow_ups = [s.get("follow_up_depth", 0) for s in session_signals]
            if follow_ups:
                avg_followup = sum(follow_ups) / len(follow_ups)
                profile.focus_depth = (profile.focus_depth + min(1.0, avg_followup / 10)) / 2

        # 启动难度：session开始前的间隔、第一条消息长度
        if len(session_signals) >= 3:
            timestamps = [s.get("timestamp", "") for s in session_signals]
            intervals = self._parse_intervals(timestamps)
            if intervals:
                avg_interval = sum(intervals) / len(intervals)
                # 间隔长=启动难（需要很长时间才能开始新任务）
                profile.startup_difficulty = min(1.0, max(0.0, avg_interval / 3600))

        # 续航模式：Git commit分布、文件修改时间分布
        if git_signals:
            hours = [s.get("hour_of_day", 12) for s in git_signals]
            if hours:
                # 如果commit集中在几个小时内→爆发型，分散→匀速型
                hour_dist = Counter(hours)
                max_count = max(hour_dist.values())
                concentration = max_count / len(hours)
                profile.endurance_mode = 1.0 - concentration  # 集中=爆发型(低分)

        # 切换弹性：多任务session比例、文件切换频率
        if session_signals:
            task_types = [s.get("task_type", "") for s in session_signals]
            unique_tasks = len(set(task_types))
            total_sessions = len(task_types)
            if total_sessions > 0:
                profile.switching_flexibility = min(1.0, unique_tasks / max(total_sessions * 0.5, 1))

        # 恢复周期：工作日vs周末activity ratio
        # 严重依赖 git，git 数据不足时标记为 insufficient
        if git_signals and git_healthy:
            weekend_count = sum(1 for s in git_signals if s.get("is_weekend"))
            total = len(git_signals)
            if total > 0:
                weekend_ratio = weekend_count / total
                # 周末也工作=快速恢复，周末不工作=需要缓冲
                profile.recovery_cycle = 1.0 - weekend_ratio
        else:
            insufficient.append("recovery_cycle")

        # Memos 笔记补充能量信号
        if memos_signals:
            # 专注深度：长笔记比例
            long_notes = sum(1 for s in memos_signals if s.get("content_length", 0) > 500)
            if len(memos_signals) > 0:
                depth_from_memos = min(1.0, long_notes / max(len(memos_signals) * 0.3, 1))
                if profile.focus_depth == 0.5:  # 未更新时保持默认
                    profile.focus_depth = depth_from_memos
                else:
                    profile.focus_depth = (profile.focus_depth + depth_from_memos) / 2

            # 切换弹性：标签多样性
            all_tags = []
            for s in memos_signals:
                tags = s.get("tags_json", "[]")
                try:
                    all_tags.extend(json.loads(tags))
                except Exception as e:
                    logger.warning(f"忽略异常: {e}")
            if all_tags:
                unique_tags = len(set(all_tags))
                profile.switching_flexibility = min(1.0, unique_tags / max(len(memos_signals) * 0.5, 1))

            # 续航模式：笔记记录时间分布
            hours = []
            for s in memos_signals:
                ts = s.get("timestamp", "")
                try:
                    h = datetime.fromisoformat(ts.replace("Z", "+00:00")).hour
                    hours.append(h)
                except Exception as e:
                    logger.warning(f"忽略异常: {e}")
            if hours:
                hour_dist = Counter(hours)
                max_count = max(hour_dist.values())
                concentration = max_count / len(hours)
                if profile.endurance_mode == 0.5:
                    profile.endurance_mode = 1.0 - concentration
                else:
                    profile.endurance_mode = (profile.endurance_mode + (1.0 - concentration)) / 2

        # 计算整体置信度
        profile.confidence = self._calculate_confidence({
            "session": len(session_signals),
            "git": len(git_signals),
            "wechat": len(wechat_signals),
            "memos": len(memos_signals) if memos_signals else 0,
        })

        profile.insufficient_dimensions = insufficient
        return profile

    # ---- Layer 2: 认知模式分析 ----

    def _analyze_cognitive(self, session_signals, git_signals, wiki_signals, memos_signals=None) -> CognitiveProfile:
        """分析认知模式"""
        profile = CognitiveProfile()
        insufficient = []

        # 数据健康度检查：关键数据源必须达到最低要求
        git_healthy = len(git_signals) >= self.MIN_SIGNALS.get("git", 5)

        # 抽象↔具象：session中概念性词汇 vs 案例性词汇比例
        if session_signals:
            abstract_keywords = ["原理", "本质", "理论", "框架", "模型", "为什么", "如何工作"]
            concrete_keywords = ["例子", "案例", "具体", "上次", "类似", "就像", "实际"]

            abstract_count = 0
            concrete_count = 0

            for s in session_signals:
                # 从final_feedback或其他字段提取（简化版）
                content = (s.get("final_feedback") or "") + " " + (s.get("selection_rationale") or "")
                abstract_count += sum(1 for kw in abstract_keywords if kw in content)
                concrete_count += sum(1 for kw in concrete_keywords if kw in content)

            total = abstract_count + concrete_count
            if total > 0:
                profile.abstraction = abstract_count / total

        # 系统↔单点：wiki链接密度、是否关注关联知识
        if wiki_signals:
            # 简化：通过知识库访问的多样性推断
            pages = [s.get("page_path", "") for s in wiki_signals]
            unique_pages = len(set(pages))
            total = len(pages)
            if total > 0:
                # 访问多样=系统视角
                profile.system_view = min(1.0, unique_pages / max(total * 0.3, 1))

        # 质疑↔信任：correction频率、是否经常挑战AI
        if session_signals:
            corrections = [s.get("correction_count", 0) for s in session_signals]
            if corrections:
                avg_correction = sum(corrections) / len(corrections)
                profile.skepticism = min(1.0, avg_correction / 3)

        # 创造↔优化：git commit类型分布
        # 严重依赖 git，git 数据不足时标记为 insufficient
        if git_signals and git_healthy:
            types = [s.get("commit_type", "") for s in git_signals]
            type_counts = Counter(types)
            creative_types = ["feat", "other"]  # 新功能
            optimize_types = ["fix", "refactor", "perf"]  # 优化
            total = len(types)
            if total > 0:
                creative_score = sum(type_counts.get(t, 0) for t in creative_types) / total
                optimize_score = sum(type_counts.get(t, 0) for t in optimize_types) / total
                if creative_score + optimize_score > 0:
                    profile.creativity = creative_score / (creative_score + optimize_score)
        else:
            insufficient.append("creativity")

        # 演绎↔归纳：commit message长度和风格
        # 严重依赖 git，git 数据不足时标记为 insufficient
        if git_signals and git_healthy:
            msg_lengths = [s.get("message_length", 0) for s in git_signals if s.get("message_length", 0) > 0]
            if msg_lengths:
                avg_length = sum(msg_lengths) / len(msg_lengths)
                # 长message通常包含更多解释（演绎），短message通常是事实陈述（归纳）
                profile.deduction = min(1.0, max(0.0, (avg_length - 20) / 80))
        else:
            insufficient.append("deduction")

        # Memos 笔记补充认知信号
        if memos_signals:
            # 结构化程度反映抽象能力：有标题+代码块+列表=更抽象/系统化
            structured = sum(1 for s in memos_signals
                             if s.get("has_title") and (s.get("has_code_block") or s.get("has_list")))
            if len(memos_signals) > 0:
                structure_ratio = structured / len(memos_signals)
                if profile.abstraction == 0.5:
                    profile.abstraction = structure_ratio
                else:
                    profile.abstraction = (profile.abstraction + structure_ratio) / 2

            # 有链接=系统视角（知识关联）
            linked = sum(1 for s in memos_signals if s.get("has_link"))
            if len(memos_signals) > 0:
                link_ratio = linked / len(memos_signals)
                if profile.system_view == 0.5:
                    profile.system_view = link_ratio
                else:
                    profile.system_view = (profile.system_view + link_ratio) / 2

        profile.confidence = self._calculate_confidence({
            "session": len(session_signals),
            "git": len(git_signals),
            "wiki": len(wiki_signals),
            "memos": len(memos_signals) if memos_signals else 0,
        })

        profile.insufficient_dimensions = insufficient
        return profile

    # ---- Layer 3: 价值优先级分析 ----

    def _analyze_value(self, session_signals, git_signals, wiki_signals, memos_signals=None) -> ValueProfile:
        """分析价值优先级"""
        profile = ValueProfile()
        insufficient = []

        # 数据健康度检查：关键数据源必须达到最低要求
        git_healthy = len(git_signals) >= self.MIN_SIGNALS.get("git", 5)

        # 正确性↔效率：termination_type分布
        if session_signals:
            terms = [s.get("termination_type", "") for s in session_signals]
            term_counts = Counter(terms)
            total = len(terms)
            if total > 0:
                # satisfied=追求正确, progress=追求效率
                correctness_signals = term_counts.get("satisfied", 0) + term_counts.get("delegated", 0)
                efficiency_signals = term_counts.get("progress", 0)
                if correctness_signals + efficiency_signals > 0:
                    profile.correctness_vs_efficiency = correctness_signals / (correctness_signals + efficiency_signals)

        # 深度↔广度：follow_up_depth分布、wiki访问模式
        if session_signals:
            depths = [s.get("follow_up_depth", 0) for s in session_signals]
            if depths:
                avg_depth = sum(depths) / len(depths)
                # 追问深=深度优先
                profile.depth_vs_breadth = min(1.0, avg_depth / 8)

        # 完美↔完成：output_type分布、文件修改频率
        if session_signals:
            outputs = [s.get("output_type", "") for s in session_signals]
            output_counts = Counter(outputs)
            total = len(outputs)
            if total > 0:
                # code/document=追求完美, discussion=快速完成
                perfect_signals = output_counts.get("code", 0) + output_counts.get("document", 0)
                complete_signals = output_counts.get("discussion", 0) + output_counts.get("decision", 0)
                if perfect_signals + complete_signals > 0:
                    profile.perfection_vs_completion = perfect_signals / (perfect_signals + complete_signals)

        # 创新↔稳妥：git commit类型、是否周末工作（side project indicator）
        # 严重依赖 git，git 数据不足时标记为 insufficient
        if git_signals and git_healthy:
            types = [s.get("commit_type", "") for s in git_signals]
            type_counts = Counter(types)
            total = len(types)
            if total > 0:
                # feat/other=创新, fix/chore=稳妥
                innov_signals = type_counts.get("feat", 0) + type_counts.get("other", 0)
                safe_signals = type_counts.get("fix", 0) + type_counts.get("chore", 0) + type_counts.get("docs", 0)
                if innov_signals + safe_signals > 0:
                    profile.innovation_vs_safety = innov_signals / (innov_signals + safe_signals)

            # 周末工作比例作为创新指标（通常side project在周末）
            weekend_ratio = sum(1 for s in git_signals if s.get("is_weekend")) / len(git_signals)
            profile.innovation_vs_safety = (profile.innovation_vs_safety + weekend_ratio) / 2
        else:
            insufficient.append("innovation_vs_safety")

        # 自主↔协作：是否有issue/PR引用（协作信号）
        # 严重依赖 git，git 数据不足时标记为 insufficient
        if git_signals and git_healthy:
            collaborative = sum(1 for s in git_signals if s.get("has_issue_reference") or s.get("has_pr_reference"))
            total = len(git_signals)
            if total > 0:
                # 有协作引用=协作优先
                profile.autonomy_vs_collaboration = 1.0 - (collaborative / total)
        else:
            insufficient.append("autonomy_vs_collaboration")

        # Memos 笔记补充价值信号
        if memos_signals:
            # 深度↔广度：长笔记比例（长笔记倾向于深度）
            long_notes = sum(1 for s in memos_signals if s.get("content_length", 0) > 1000)
            if len(memos_signals) > 0:
                depth_ratio = min(1.0, long_notes / max(len(memos_signals) * 0.2, 1))
                if profile.depth_vs_breadth == 0.5:
                    profile.depth_vs_breadth = depth_ratio
                else:
                    profile.depth_vs_breadth = (profile.depth_vs_breadth + depth_ratio) / 2

            # 完美↔完成：有代码块+图片=追求完美（精心整理）
            rich_notes = sum(1 for s in memos_signals
                             if s.get("has_code_block") or s.get("image_count", 0) > 0)
            if len(memos_signals) > 0:
                perfect_ratio = rich_notes / len(memos_signals)
                if profile.perfection_vs_completion == 0.5:
                    profile.perfection_vs_completion = perfect_ratio
                else:
                    profile.perfection_vs_completion = (profile.perfection_vs_completion + perfect_ratio) / 2

        profile.confidence = self._calculate_confidence({
            "session": len(session_signals),
            "git": len(git_signals),
            "wiki": len(wiki_signals),
            "memos": len(memos_signals) if memos_signals else 0,
        })

        profile.insufficient_dimensions = insufficient
        return profile

    # ---- 辅助方法 ----

    def _calculate_confidence(self, signal_counts: Dict[str, int]) -> float:
        """计算画像置信度"""
        confidence = 0.0
        for source, count in signal_counts.items():
            min_required = self.MIN_SIGNALS.get(source, 10)
            source_confidence = min(1.0, count / min_required)
            confidence += source_confidence

        return min(1.0, confidence / max(len(signal_counts), 1))

    def _calculate_changes(self, energy: EnergyProfile, cognitive: CognitiveProfile,
                           value: ValueProfile, previous: PreferenceProfile):
        """计算与上一周期的变化，更新各维度的变化标签。"""
        if not previous:
            return

        # 定义变化阈值
        SIGNIFICANT = 0.15
        MAJOR = 0.25

        def calc_change(current: float, prev: float) -> str:
            delta = current - prev
            if abs(delta) < SIGNIFICANT:
                return "stable"
            direction = "up" if delta > 0 else "down"
            magnitude = "major" if abs(delta) >= MAJOR else "significant"
            return f"{direction}_{magnitude}"

        # 能量层变化
        energy._changes = {  # type: ignore
            "focus_depth": calc_change(energy.focus_depth, previous.energy.focus_depth),
            "startup_difficulty": calc_change(energy.startup_difficulty, previous.energy.startup_difficulty),
            "endurance_mode": calc_change(energy.endurance_mode, previous.energy.endurance_mode),
            "switching_flexibility": calc_change(energy.switching_flexibility, previous.energy.switching_flexibility),
            "recovery_cycle": calc_change(energy.recovery_cycle, previous.energy.recovery_cycle),
        }

        # 认知层变化
        cognitive._changes = {  # type: ignore
            "abstraction": calc_change(cognitive.abstraction, previous.cognitive.abstraction),
            "system_view": calc_change(cognitive.system_view, previous.cognitive.system_view),
            "skepticism": calc_change(cognitive.skepticism, previous.cognitive.skepticism),
            "creativity": calc_change(cognitive.creativity, previous.cognitive.creativity),
            "deduction": calc_change(cognitive.deduction, previous.cognitive.deduction),
        }

        # 价值层变化
        value._changes = {  # type: ignore
            "correctness_vs_efficiency": calc_change(value.correctness_vs_efficiency, previous.value.correctness_vs_efficiency),
            "depth_vs_breadth": calc_change(value.depth_vs_breadth, previous.value.depth_vs_breadth),
            "perfection_vs_completion": calc_change(value.perfection_vs_completion, previous.value.perfection_vs_completion),
            "innovation_vs_safety": calc_change(value.innovation_vs_safety, previous.value.innovation_vs_safety),
            "autonomy_vs_collaboration": calc_change(value.autonomy_vs_collaboration, previous.value.autonomy_vs_collaboration),
        }

    def detect_drift(self, current: PreferenceProfile,
                     previous: PreferenceProfile = None) -> List[Dict]:
        """
        检测画像漂移。

        漂移类型：
        1. sudden_shift: 单维度变化 > 0.25，可能是噪声或重大生活变化
        2. gradual_drift: 多维度同向缓慢偏移 > 0.15，偏好确实在演化
        3. update_lag: 画像版本过旧（> 120天未更新）
        4. low_confidence_drift: 高变化 + 低置信度 = 数据不足，不应过度解读

        Returns:
            漂移警报列表
        """
        alerts = []

        if not previous:
            return alerts

        # 检查所有维度的变化
        dimensions = [
            ("energy.focus_depth", previous.energy.focus_depth, current.energy.focus_depth),
            ("energy.startup_difficulty", previous.energy.startup_difficulty, current.energy.startup_difficulty),
            ("energy.endurance_mode", previous.energy.endurance_mode, current.energy.endurance_mode),
            ("energy.switching_flexibility", previous.energy.switching_flexibility, current.energy.switching_flexibility),
            ("energy.recovery_cycle", previous.energy.recovery_cycle, current.energy.recovery_cycle),
            ("cognitive.abstraction", previous.cognitive.abstraction, current.cognitive.abstraction),
            ("cognitive.system_view", previous.cognitive.system_view, current.cognitive.system_view),
            ("cognitive.skepticism", previous.cognitive.skepticism, current.cognitive.skepticism),
            ("cognitive.creativity", previous.cognitive.creativity, current.cognitive.creativity),
            ("cognitive.deduction", previous.cognitive.deduction, current.cognitive.deduction),
            ("value.correctness_vs_efficiency", previous.value.correctness_vs_efficiency, current.value.correctness_vs_efficiency),
            ("value.depth_vs_breadth", previous.value.depth_vs_breadth, current.value.depth_vs_breadth),
            ("value.perfection_vs_completion", previous.value.perfection_vs_completion, current.value.perfection_vs_completion),
            ("value.innovation_vs_safety", previous.value.innovation_vs_safety, current.value.innovation_vs_safety),
            ("value.autonomy_vs_collaboration", previous.value.autonomy_vs_collaboration, current.value.autonomy_vs_collaboration),
        ]

        sudden_shifts = []
        gradual_drifts = []

        for name, prev, curr in dimensions:
            delta = curr - prev
            if abs(delta) > 0.25:
                sudden_shifts.append((name, prev, curr, delta))
            elif abs(delta) > 0.15:
                gradual_drifts.append((name, prev, curr, delta))

        # 类型1： sudden_shift
        for name, prev, curr, delta in sudden_shifts:
            # 如果整体置信度低，标记为噪声
            avg_confidence = (current.energy.confidence + current.cognitive.confidence +
                              current.value.confidence) / 3
            if avg_confidence < 0.4:
                alert_type = "low_confidence_drift"
                advice = "画像置信度低，此次变化可能是数据不足导致的噪声，建议继续观察"
            else:
                alert_type = "sudden_shift"
                advice = "单维度发生剧烈变化，可能反映了重大情境变化（如换工作、新项目），建议审视是否为持久变化"

            alerts.append({
                "type": alert_type,
                "dimension": name,
                "previous": round(prev, 2),
                "current": round(curr, 2),
                "delta": round(delta, 2),
                "severity": "high" if abs(delta) > 0.35 else "medium",
                "advice": advice,
            })

        # 类型2： gradual_drift（多维度同向偏移）
        if len(gradual_drifts) >= 3:
            # 检查是否同向
            positive = sum(1 for _, _, _, d in gradual_drifts if d > 0)
            negative = sum(1 for _, _, _, d in gradual_drifts if d < 0)
            if positive >= 3 or negative >= 3:
                direction = "上升" if positive > negative else "下降"
                alerts.append({
                    "type": "gradual_drift",
                    "dimension": f"{len(gradual_drifts)}个维度同向{direction}",
                    "previous": None,
                    "current": None,
                    "delta": None,
                    "severity": "medium",
                    "advice": f"多个维度同时{direction}，偏好正在演化。建议在下个季度关注这些变化是否持续",
                })

        # 类型3： update_lag
        try:
            prev_date = datetime.fromisoformat(previous.generated_at.replace('Z', '+00:00'))
            days_since = (datetime.now() - prev_date).days
            if days_since > 120:
                alerts.append({
                    "type": "update_lag",
                    "dimension": "画像更新",
                    "previous": f"{days_since}天前",
                    "current": "现在",
                    "delta": days_since,
                    "severity": "medium",
                    "advice": "画像已超过120天未更新，可能无法反映当前偏好，建议尽快重新分析",
                })
        except Exception as e:
            logger.warning(f"忽略异常: {e}")

        return alerts

    def _parse_intervals(self, timestamps: List[str]) -> List[float]:
        """解析时间戳间隔（秒）"""
        intervals = []
        try:
            parsed = []
            for ts in timestamps:
                try:
                    dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
                    parsed.append(dt)
                except Exception:
                    continue

            parsed.sort()
            for i in range(1, len(parsed)):
                delta = (parsed[i] - parsed[i-1]).total_seconds()
                if 60 < delta < 86400 * 7:  # 过滤异常值
                    intervals.append(delta)
        except Exception as e:
            logger.warning(f"忽略异常: {e}")
        return intervals

    def _get_git_signals(self, days: int) -> List[Dict]:
        """获取Git信号"""
        try:
            import sqlite3
            with sqlite3.connect(str(self.store.db_path)) as conn:
                conn.row_factory = sqlite3.Row
                cursor = conn.execute("""
                    SELECT * FROM git_signals
                    WHERE timestamp >= date('now', ?)
                    ORDER BY timestamp DESC
                """, (f'-{days} days',))
                return [dict(row) for row in cursor.fetchall()]
        except Exception:
            return []

    def _get_wiki_signals(self, days: int) -> List[Dict]:
        """获取Wiki信号"""
        try:
            import sqlite3
            with sqlite3.connect(str(self.store.db_path)) as conn:
                conn.row_factory = sqlite3.Row
                cursor = conn.execute("""
                    SELECT * FROM knowledge_signals
                    WHERE timestamp >= date('now', ?)
                    ORDER BY timestamp DESC
                """, (f'-{days} days',))
                return [dict(row) for row in cursor.fetchall()]
        except Exception:
            return []

    def _get_fs_signals(self, days: int) -> List[Dict]:
        """获取文件系统信号"""
        try:
            import sqlite3
            with sqlite3.connect(str(self.store.db_path)) as conn:
                conn.row_factory = sqlite3.Row
                cursor = conn.execute("""
                    SELECT * FROM file_system_signals
                    WHERE timestamp >= date('now', ?)
                    ORDER BY timestamp DESC
                """, (f'-{days} days',))
                return [dict(row) for row in cursor.fetchall()]
        except Exception:
            return []


# ========== 便捷函数 ==========

def analyze_preferences(days: int = 90) -> PreferenceProfile:
    """便捷函数：分析偏好画像"""
    analyzer = PreferenceAnalyzer()
    return analyzer.analyze(days=days)


def generate_radar_report(profile: PreferenceProfile = None) -> str:
    """生成雷达图文本报告"""
    if profile is None:
        profile = analyze_preferences()

    data = profile.to_dict()
    lines = [
        f"# 用户偏好画像 v{profile.version}",
        f"生成时间: {profile.generated_at[:10]} | 信号数: {profile.signal_count}",
        "",
        "## Layer 1: 能量模式（How you work）",
        "",
    ]

    for key, val in data["energy"].items():
        if key == "confidence":
            lines.append(f"置信度: {val}")
            continue
        score = val["score"]
        label = val["label"]
        bar = "█" * int(score * 10) + "░" * (10 - int(score * 10))
        lines.append(f"{key}: [{bar}] {score:.2f} → {label}")

    lines.extend(["", "## Layer 2: 认知模式（How you think）", ""])
    for key, val in data["cognitive"].items():
        if key == "confidence":
            lines.append(f"置信度: {val}")
            continue
        score = val["score"]
        label = val["label"]
        bar = "█" * int(score * 10) + "░" * (10 - int(score * 10))
        lines.append(f"{key}: [{bar}] {score:.2f} → {label}")

    lines.extend(["", "## Layer 3: 价值优先级（What you care）", ""])
    for key, val in data["value"].items():
        if key == "confidence":
            lines.append(f"置信度: {val}")
            continue
        score = val["score"]
        label = val["label"]
        bar = "█" * int(score * 10) + "░" * (10 - int(score * 10))
        lines.append(f"{key}: [{bar}] {score:.2f} → {label}")

    return "\n".join(lines)


if __name__ == "__main__":
    print(generate_radar_report())
