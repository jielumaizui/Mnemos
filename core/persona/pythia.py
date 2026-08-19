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


import sqlite3
from typing import Dict, List, Tuple
from datetime import datetime, timedelta
from collections import Counter, defaultdict
from dataclasses import fields

from .psyche import SignalStore, get_signal_store
from .pythia_profiles import (
    CognitiveProfile,
    EnergyProfile,
    PreferenceProfile,
    ValueProfile,
    _BehaviorCalibrator,
    _DomainPreferenceAnalyzer,
    _PercentileNormalizer,
)
import logging

from core.cognitive.user_model_asset_store import InteractionPreferenceStore
from core.cognitive.user_model_assets import (
    AssetScope,
    CognitiveAuthorityEvidence,
    InteractionPreference,
)
from core.config import get_config
from core.evidence.source_authority import SourceAuthorityCatalog

# Constants extracted from magic numbers
PREFERENCE_ANALYZER_STARTUP_DIFFICULTY_SCALE_SECONDS = 3600
PREFERENCE_ANALYZER_INTERVAL_FILTER_MAX_SECONDS = 86400
PREFERENCE_ANALYZER_INTERVAL_FILTER_MAX_SECONDS_2 = 30
PREFERENCE_ANALYZER_ANALYZE_PREFERENCE_PROFILE_DAYS = 90
PREFERENCE_ANALYZER__FALLBACK_FROM_KNOWLEDGE_PROFILE_PREFERENCE_PROFILE_DAYS = 90
ANALYZE_PREFERENCES_PREFERENCE_PROFILE_DAYS = 90
PREFERENCE_PROFILE_MATERIALITY_DELTA = 0.05


logger = logging.getLogger(__name__)

# ========== 分析引擎 ==========


class PreferenceAnalyzer:
    """偏好画像分析引擎"""

    # 最小信号数阈值
    MIN_SIGNALS = {
        "session": 10,
        "git": 5,
        "wiki": 5,
    }

    # 画像推断常量
    STARTUP_DIFFICULTY_SCALE_SECONDS = (
        PREFERENCE_ANALYZER_STARTUP_DIFFICULTY_SCALE_SECONDS  # 启动难度缩放：1小时
    )
    UPDATE_LAG_DAYS_THRESHOLD = 120  # 画像过期阈值：120天
    INTERVAL_FILTER_MIN_SECONDS = 60  # 间隔过滤下限：1分钟
    INTERVAL_FILTER_MAX_SECONDS = (
        PREFERENCE_ANALYZER_INTERVAL_FILTER_MAX_SECONDS
        * PREFERENCE_ANALYZER_INTERVAL_FILTER_MAX_SECONDS_2
    )  # 间隔过滤上限：30天（覆盖常见休假周期）

    def __init__(
        self,
        store: SignalStore | None = None,
        *,
        interaction_preference_store: InteractionPreferenceStore | None = None,
    ):
        self.store = store or get_signal_store()
        self.interaction_preference_store = (
            interaction_preference_store
            or InteractionPreferenceStore(get_config().database_dir / "interaction_preferences.db")
        )
        self._percentile = _PercentileNormalizer(window_size=100)
        self._calibrator = _BehaviorCalibrator()
        self._domain_analyzer = _DomainPreferenceAnalyzer()

    def record_interaction_preference(
        self,
        *,
        dimension: str,
        value: str,
        scope: AssetScope,
        confidence: float,
        expires_at: str,
        invalidation_condition: str,
        evidence: tuple[CognitiveAuthorityEvidence, ...],
        source_authority_catalog: SourceAuthorityCatalog,
    ) -> InteractionPreference:
        """Persist one explicit scoped preference through Pythia's owner path."""

        preference = InteractionPreference.create(
            dimension=dimension,
            value=value,
            evidence_refs=tuple(item.evidence_ref for item in evidence),
            scope=scope,
            confidence=confidence,
            expires_at=expires_at,
            invalidation_condition=invalidation_condition,
            authority_evidence_refs=tuple(item.evidence_ref for item in evidence),
        )
        self.interaction_preference_store.persist(
            preference,
            evidence=evidence,
            catalog=source_authority_catalog,
        )
        return preference

    def invalidate_interaction_preference(
        self,
        preference: InteractionPreference,
        *,
        evidence: tuple[CognitiveAuthorityEvidence, ...],
        source_authority_catalog: SourceAuthorityCatalog,
    ) -> InteractionPreference:
        """Invalidate the exact current preference revision with typed evidence."""

        return self.interaction_preference_store.transition_preference(
            preference.asset_id,
            expected_revision_id=preference.revision_id,
            next_status="invalidated",
            evidence=evidence,
            catalog=source_authority_catalog,
        )

    def analyze(
        self,
        days: int = PREFERENCE_ANALYZER_ANALYZE_PREFERENCE_PROFILE_DAYS,
        previous_profile: PreferenceProfile | None = None,
        incremental: bool = True,
        metis_profile=None,
    ) -> PreferenceProfile:
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
        wechat_signals = []  # type: ignore[var-annotated]
        fs_signals = self._get_fs_signals(days=days)
        reflection_signals = self._get_reflection_signals(days=days)

        total_signals = (
            len(session_signals) + len(git_signals) + len(wiki_signals) + len(reflection_signals)
        )

        if total_signals == 0 and metis_profile is not None:
            return self._fallback_from_knowledge_profile(metis_profile, previous_profile, days)

        # 三层雷达分析
        energy = self._analyze_energy(session_signals, git_signals, wechat_signals, fs_signals)
        cognitive = self._analyze_cognitive(session_signals, git_signals, wiki_signals)
        value = self._analyze_value(session_signals, git_signals, wiki_signals)
        domain_preferences = self._domain_analyzer.analyze(session_signals, wiki_signals)

        # P109: 将 Layer 5 反射信号纳入认知/价值画像
        if reflection_signals:
            cognitive, value = self._apply_reflection_signals(cognitive, value, reflection_signals)

        if previous_profile:
            self._calculate_changes(energy, cognitive, value, previous_profile)

        profile = PreferenceProfile(
            version=(previous_profile.version + 1) if previous_profile else 1,
            generated_at=datetime.now().isoformat(),
            period_start=(datetime.now() - timedelta(days=days)).isoformat()[:10],
            period_end=datetime.now().isoformat()[:10],
            energy=energy,
            cognitive=cognitive,
            value=value,
            signal_count=total_signals,
            domain_preferences=domain_preferences,
        )
        profile.source_signal_ids = self._source_signal_ids(
            session_signals,
            git_signals,
            wiki_signals,
            wechat_signals,
            fs_signals,
        )
        return profile

    @staticmethod
    def is_material_change(
        previous: PreferenceProfile,
        candidate: PreferenceProfile,
        *,
        score_delta: float = PREFERENCE_PROFILE_MATERIALITY_DELTA,
    ) -> bool:
        """Require a meaningful profile change before emitting a new revision."""

        for layer_name in ("energy", "cognitive", "value"):
            previous_layer = getattr(previous, layer_name)
            candidate_layer = getattr(candidate, layer_name)
            for field_spec in fields(previous_layer):
                old_value = getattr(previous_layer, field_spec.name)
                new_value = getattr(candidate_layer, field_spec.name)
                if field_spec.name == "confidence":
                    continue
                if isinstance(old_value, (int, float)) and isinstance(new_value, (int, float)):
                    if abs(float(old_value) - float(new_value)) >= score_delta:
                        return True
                elif old_value != new_value:
                    return True
        keys = set(previous.domain_preferences) | set(candidate.domain_preferences)
        return any(
            abs(
                float(candidate.domain_preferences.get(key, 0.0))
                - float(previous.domain_preferences.get(key, 0.0))
            )
            >= score_delta
            for key in keys
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
        new_wechat = []  # type: ignore[var-annotated]
        new_fs = []  # type: ignore[var-annotated]
        # P109: 增量模式读取自上一画像生成以来的 reflection 信号
        new_reflection = self._get_reflection_signals_since(previous.generated_at)

        total_new = (
            len(new_session)
            + len(new_git)
            + len(new_wiki)
            + len(new_wechat)
            + len(new_fs)
            + len(new_reflection)
        )
        if total_new == 0:
            return previous

        # 基于新信号计算增量分数
        delta_energy = self._analyze_energy(new_session, new_git, new_wechat, new_fs)
        delta_cognitive = self._analyze_cognitive(new_session, new_git, new_wiki)
        delta_value = self._analyze_value(new_session, new_git, new_wiki)
        delta_domain_preferences = self._domain_analyzer.analyze(new_session, new_wiki)

        # P109: 将增量 reflection 信号纳入认知/价值画像
        if new_reflection:
            delta_cognitive, delta_value = self._apply_reflection_signals(
                delta_cognitive, delta_value, new_reflection
            )

        # 计算各源权重（用于加权平均）
        old_weight = max(1, previous.signal_count)
        new_weight = max(1, total_new)
        total_weight = old_weight + new_weight

        def merge_score(old: float, new: float) -> float:
            return (old * old_weight + new * new_weight) / total_weight

        # 合并 insufficient_dimensions：新数据仍不足或旧数据不足的都保留
        # 修复：使用并集而非交集，只要任一周期数据不足就标记为 insufficient
        prev_ins_energy = set(previous.energy.insufficient_dimensions or [])
        delta_ins_energy = set(delta_energy.insufficient_dimensions or [])
        merged_ins_energy = sorted(prev_ins_energy | delta_ins_energy)

        prev_ins_cognitive = set(previous.cognitive.insufficient_dimensions or [])
        delta_ins_cognitive = set(delta_cognitive.insufficient_dimensions or [])
        merged_ins_cognitive = sorted(prev_ins_cognitive | delta_ins_cognitive)

        prev_ins_value = set(previous.value.insufficient_dimensions or [])
        delta_ins_value = set(delta_value.insufficient_dimensions or [])
        merged_ins_value = sorted(prev_ins_value | delta_ins_value)

        # 合并能量层
        energy = EnergyProfile(
            focus_depth=merge_score(previous.energy.focus_depth, delta_energy.focus_depth),
            startup_difficulty=merge_score(
                previous.energy.startup_difficulty, delta_energy.startup_difficulty
            ),
            endurance_mode=merge_score(previous.energy.endurance_mode, delta_energy.endurance_mode),
            switching_flexibility=merge_score(
                previous.energy.switching_flexibility, delta_energy.switching_flexibility
            ),
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
            correctness_vs_efficiency=merge_score(
                previous.value.correctness_vs_efficiency, delta_value.correctness_vs_efficiency
            ),
            depth_vs_breadth=merge_score(
                previous.value.depth_vs_breadth, delta_value.depth_vs_breadth
            ),
            perfection_vs_completion=merge_score(
                previous.value.perfection_vs_completion, delta_value.perfection_vs_completion
            ),
            innovation_vs_safety=merge_score(
                previous.value.innovation_vs_safety, delta_value.innovation_vs_safety
            ),
            autonomy_vs_collaboration=merge_score(
                previous.value.autonomy_vs_collaboration, delta_value.autonomy_vs_collaboration
            ),
            action_vs_analysis=merge_score(
                previous.value.action_vs_analysis, delta_value.action_vs_analysis
            ),
            confidence=min(1.0, previous.value.confidence + 0.05),
            insufficient_dimensions=merged_ins_value,
        )

        # 计算变化
        self._calculate_changes(energy, cognitive, value, previous)

        profile = PreferenceProfile(
            version=previous.version + 1,
            generated_at=datetime.now().isoformat(),
            period_start=previous.period_start,
            period_end=datetime.now().isoformat()[:10],
            energy=energy,
            cognitive=cognitive,
            value=value,
            signal_count=previous.signal_count + total_new,
            domain_preferences=self._merge_domain_preferences(
                previous.domain_preferences,
                delta_domain_preferences,
            ),
        )
        profile.source_signal_ids = self._source_signal_ids(
            new_session,
            new_git,
            new_wiki,
            new_wechat,
            new_fs,
        )
        return profile

    @staticmethod
    def _source_signal_ids(session, git, wiki, wechat, fs) -> Dict[str, List[int]]:
        """Return the exact unconsumed signal cursor for a candidate revision."""

        sources = {
            "session": session,
            "git": git,
            "knowledge": wiki,
            "wechat": wechat,
            "file_system": fs,
        }
        return {
            source_type: sorted({int(item["id"]) for item in signals if "id" in item})
            for source_type, signals in sources.items()
            if signals
        }

    def _fallback_from_knowledge_profile(
        self,
        metis_profile,
        previous_profile: PreferenceProfile | None = None,
        days: int = PREFERENCE_ANALYZER__FALLBACK_FROM_KNOWLEDGE_PROFILE_PREFERENCE_PROFILE_DAYS,
    ) -> PreferenceProfile:
        """行为信号不足时，用知识画像做低置信度降级推断。"""
        profile = PreferenceProfile(
            version=(previous_profile.version + 1) if previous_profile else 1,
            generated_at=datetime.now().isoformat(),
            period_start=(datetime.now() - timedelta(days=days)).isoformat()[:10],
            period_end=datetime.now().isoformat()[:10],
            signal_count=0,
        )

        entropy = getattr(metis_profile, "domain_entropy", 0.0)
        profile.value.depth_vs_breadth = round(1.0 - min(1.0, entropy), 2)

        learning = getattr(metis_profile, "learning_mode", {}) or {}
        simple_mode = (
            learning.get("simple_mode", "") if isinstance(learning, dict) else str(learning)
        )
        if "方法论" in simple_mode or "deductive" in simple_mode:
            profile.cognitive.abstraction = 0.7
            profile.cognitive.deduction = 0.7
        elif "问题" in simple_mode or "inductive" in simple_mode:
            profile.cognitive.abstraction = 0.35
            profile.cognitive.deduction = 0.35

        tool_stack = getattr(metis_profile, "tool_stack", []) or []
        if len(tool_stack) > 3:
            profile.cognitive.system_view = min(1.0, len(tool_stack) / 10)

        profile.energy.confidence = 0.1
        profile.cognitive.confidence = 0.3
        profile.value.confidence = 0.3
        profile.energy.insufficient_dimensions = [
            "focus_depth",
            "startup_difficulty",
            "endurance_mode",
            "switching_flexibility",
            "recovery_cycle",
        ]
        profile.cognitive.insufficient_dimensions = ["skepticism", "creativity"]
        profile.value.insufficient_dimensions = [
            "correctness_vs_efficiency",
            "perfection_vs_completion",
            "innovation_vs_safety",
            "autonomy_vs_collaboration",
            "action_vs_analysis",
        ]
        return profile

    # ---- Layer 1: 能量模式分析 ----

    def _analyze_energy(
        self, session_signals, git_signals, wechat_signals, fs_signals
    ) -> EnergyProfile:
        """分析能量模式（使用分位数归一化 + 行为校准）"""
        profile = EnergyProfile()
        git_healthy = len(git_signals) >= self.MIN_SIGNALS.get("git", 5)

        profile.focus_depth = self._calc_focus_depth(session_signals)
        profile.startup_difficulty = self._calc_startup_difficulty(session_signals)
        profile.endurance_mode = self._calc_endurance_mode(git_signals)
        profile.switching_flexibility = self._calc_switching_flexibility(session_signals)
        profile.recovery_cycle, recovery_insufficient = self._calc_recovery_cycle(
            git_signals, git_healthy
        )

        self._apply_energy_calibration(profile, session_signals)
        self._clamp_energy_scores(profile)

        profile.confidence = self._calculate_confidence(
            {
                "session": len(session_signals),
                "git": len(git_signals),
                "wechat": len(wechat_signals),
            }
        )

        profile.insufficient_dimensions = ["recovery_cycle"] if recovery_insufficient else []
        return profile

    def _calc_focus_depth(self, session_signals) -> float:
        """专注深度：session 持续时间 + 追问深度。"""
        if not session_signals:
            return 0.5

        focus_depth = 0.5
        durations = [
            s.get("duration_seconds", 0)
            for s in session_signals
            if s.get("duration_seconds", 0) > 0
        ]
        if durations:
            duration_scores = self._percentile.batch_normalize("energy_duration", durations)
            focus_depth = sum(duration_scores) / len(duration_scores)

        follow_ups = [s.get("follow_up_depth", 0) for s in session_signals]
        if follow_ups:
            followup_scores = self._percentile.batch_normalize("energy_followup", follow_ups)
            avg_followup_score = sum(followup_scores) / len(followup_scores)
            focus_depth = (focus_depth + avg_followup_score) / 2

        return focus_depth

    def _calc_startup_difficulty(self, session_signals) -> float:
        """启动难度：session 间隔（分位数归一化）。"""
        if len(session_signals) < 3:
            return 0.5

        timestamps = [s.get("timestamp", "") for s in session_signals]
        intervals = self._parse_intervals(timestamps)
        if not intervals:
            return 0.5

        interval_scores = self._percentile.batch_normalize("energy_interval", intervals)
        return sum(interval_scores) / len(interval_scores)

    def _calc_endurance_mode(self, git_signals) -> float:
        """续航模式：Git commit 时间分布集中度。"""
        if not git_signals:
            return 0.5

        hours = [s.get("hour_of_day", 12) for s in git_signals]
        if not hours:
            return 0.5

        hour_dist = Counter(hours)
        max_count = max(hour_dist.values())
        concentration = max_count / len(hours)
        return 1.0 - concentration

    def _calc_switching_flexibility(self, session_signals) -> float:
        """切换弹性：多任务比例（分位数归一化）。"""
        if not session_signals:
            return 0.5

        task_types = [s.get("task_type", "") for s in session_signals]
        unique_tasks = len(set(task_types))
        total_sessions = len(task_types)
        if total_sessions == 0:
            return 0.5

        raw_ratio = unique_tasks / total_sessions
        self._percentile.update("energy_switching", [raw_ratio])
        return self._percentile.normalize("energy_switching", raw_ratio)

    def _calc_recovery_cycle(self, git_signals, git_healthy: bool) -> Tuple[float, bool]:
        """恢复周期：工作日 vs 周末。返回 (score, is_insufficient)。"""
        if not git_signals or not git_healthy:
            return 0.5, True

        weekend_count = sum(1 for s in git_signals if s.get("is_weekend"))
        total = len(git_signals)
        if total == 0:
            return 0.5, True

        weekend_ratio = weekend_count / total
        return 1.0 - weekend_ratio, False

    def _apply_energy_calibration(self, profile: EnergyProfile, session_signals) -> None:
        """行为校准：根据用户行为自动微调能量分数。"""
        if not session_signals:
            return

        calibrations = self._calibrator.calibrate(session_signals, "energy")
        if "focus_depth" in calibrations:
            profile.focus_depth = self._calibrator.apply(
                profile.focus_depth, calibrations["focus_depth"]
            )

    def _clamp_energy_scores(self, profile: EnergyProfile) -> None:
        """确保所有能量分数在 [0, 1] 范围内。"""
        profile.focus_depth = max(0.0, min(1.0, profile.focus_depth))
        profile.startup_difficulty = max(0.0, min(1.0, profile.startup_difficulty))
        profile.endurance_mode = max(0.0, min(1.0, profile.endurance_mode))
        profile.switching_flexibility = max(0.0, min(1.0, profile.switching_flexibility))
        profile.recovery_cycle = max(0.0, min(1.0, profile.recovery_cycle))

    # ---- Layer 2: 认知模式分析 ----

    _ABSTRACT_KEYWORDS = (
        "原理",
        "本质",
        "理论",
        "框架",
        "模型",
        "为什么",
        "如何工作",
        "principle",
        "theory",
        "framework",
        "model",
        "mechanism",
    )
    _CONCRETE_KEYWORDS = (
        "例子",
        "案例",
        "具体",
        "上次",
        "类似",
        "就像",
        "实际",
        "example",
        "case",
        "specific",
        "practical",
        "demo",
    )
    _CREATIVE_COMMIT_TYPES = ("feat", "other")
    _OPTIMIZE_COMMIT_TYPES = ("fix", "refactor", "perf")
    _COGNITIVE_DIMENSIONS = ("abstraction", "system_view", "skepticism", "creativity", "deduction")

    def _count_keyword_matches(self, content: str, keywords: tuple[str, ...]) -> int:
        return sum(1 for kw in keywords if kw in content)

    def _analyze_abstraction(self, profile: CognitiveProfile, session_signals) -> None:
        if not session_signals:
            return
        abstract_scores = []
        concrete_scores = []
        for s in session_signals:
            content = (s.get("final_feedback") or "") + " " + (s.get("selection_rationale") or "")
            abstract_scores.append(self._count_keyword_matches(content, self._ABSTRACT_KEYWORDS))
            concrete_scores.append(self._count_keyword_matches(content, self._CONCRETE_KEYWORDS))

        if not abstract_scores or not concrete_scores:
            return
        abs_norm = self._percentile.batch_normalize(
            "cognitive_abstract", abstract_scores  # type: ignore[arg-type]
        )  # type: ignore[arg-type]
        con_norm = self._percentile.batch_normalize(
            "cognitive_concrete", concrete_scores  # type: ignore[arg-type]
        )  # type: ignore[arg-type]
        avg_abs = sum(abs_norm) / len(abs_norm)
        avg_con = sum(con_norm) / len(con_norm)
        total = avg_abs + avg_con
        if total > 0:
            profile.abstraction = avg_abs / total

    def _analyze_system_view(self, profile: CognitiveProfile, wiki_signals) -> None:
        if not wiki_signals:
            return
        pages = [s.get("page_path", "") for s in wiki_signals]
        unique_pages = len(set(pages))
        total = len(pages)
        if total == 0:
            return
        raw_ratio = unique_pages / total
        self._percentile.update("cognitive_system", [raw_ratio])
        profile.system_view = self._percentile.normalize("cognitive_system", raw_ratio)

    def _analyze_skepticism(self, profile: CognitiveProfile, session_signals) -> None:
        if not session_signals:
            return
        corrections = [s.get("correction_count", 0) for s in session_signals]
        if not corrections:
            return
        corr_scores = self._percentile.batch_normalize("cognitive_correction", corrections)
        profile.skepticism = sum(corr_scores) / len(corr_scores)

    def _analyze_creativity(
        self, profile: CognitiveProfile, git_signals, git_healthy: bool, insufficient: list
    ) -> None:
        if not git_signals or not git_healthy:
            insufficient.append("creativity")
            return
        types = [s.get("commit_type", "") for s in git_signals]
        type_counts = Counter(types)
        total = len(types)
        if total == 0:
            insufficient.append("creativity")
            return
        creative_score = sum(type_counts.get(t, 0) for t in self._CREATIVE_COMMIT_TYPES) / total
        optimize_score = sum(type_counts.get(t, 0) for t in self._OPTIMIZE_COMMIT_TYPES) / total
        if creative_score + optimize_score > 0:
            profile.creativity = creative_score / (creative_score + optimize_score)

    def _analyze_deduction(
        self, profile: CognitiveProfile, git_signals, git_healthy: bool, insufficient: list
    ) -> None:
        if not git_signals or not git_healthy:
            insufficient.append("deduction")
            return
        msg_lengths = [
            s.get("message_length", 0) for s in git_signals if s.get("message_length", 0) > 0
        ]
        if not msg_lengths:
            insufficient.append("deduction")
            return
        length_scores = self._percentile.batch_normalize("cognitive_msg_length", msg_lengths)
        profile.deduction = sum(length_scores) / len(length_scores)

    def _apply_cognitive_calibration(self, profile: CognitiveProfile, session_signals) -> None:
        if not session_signals:
            return
        calibrations = self._calibrator.calibrate(session_signals, "cognitive")
        for dim, adj in calibrations.items():
            if hasattr(profile, dim):
                setattr(profile, dim, self._calibrator.apply(getattr(profile, dim), adj))

    def _clamp_cognitive_scores(self, profile: CognitiveProfile) -> None:
        for dim in self._COGNITIVE_DIMENSIONS:
            value = getattr(profile, dim)
            setattr(profile, dim, max(0.0, min(1.0, value)))

    def _analyze_cognitive(self, session_signals, git_signals, wiki_signals) -> CognitiveProfile:
        """分析认知模式（使用分位数归一化 + 行为校准）"""
        profile = CognitiveProfile()
        insufficient: list = []
        git_healthy = len(git_signals) >= self.MIN_SIGNALS.get("git", 5)

        self._analyze_abstraction(profile, session_signals)
        self._analyze_system_view(profile, wiki_signals)
        self._analyze_skepticism(profile, session_signals)
        self._analyze_creativity(profile, git_signals, git_healthy, insufficient)
        self._analyze_deduction(profile, git_signals, git_healthy, insufficient)
        self._apply_cognitive_calibration(profile, session_signals)
        self._clamp_cognitive_scores(profile)

        profile.confidence = self._calculate_confidence(
            {
                "session": len(session_signals),
                "git": len(git_signals),
                "wiki": len(wiki_signals),
            }
        )

        profile.insufficient_dimensions = insufficient
        return profile

    # ---- Layer 3: 价值优先级分析 ----

    def _analyze_correctness_vs_efficiency(self, profile: ValueProfile, session_signals) -> None:
        if not session_signals:
            return
        terms = [s.get("termination_type", "") for s in session_signals]
        term_counts = Counter(terms)
        total = len(terms)
        if total == 0:
            return
        correctness_signals = term_counts.get("satisfied", 0) + term_counts.get("delegated", 0)
        raw_correctness = correctness_signals / total
        self._percentile.update("value_correctness", [raw_correctness])
        profile.correctness_vs_efficiency = self._percentile.normalize(
            "value_correctness", raw_correctness
        )

    def _analyze_depth_vs_breadth(self, profile: ValueProfile, session_signals) -> None:
        if not session_signals:
            return
        depths = [s.get("follow_up_depth", 0) for s in session_signals]
        if not depths:
            return
        depth_scores = self._percentile.batch_normalize("value_depth", depths)
        profile.depth_vs_breadth = sum(depth_scores) / len(depth_scores)

    def _analyze_perfection_vs_completion(self, profile: ValueProfile, session_signals) -> None:
        if not session_signals:
            return
        outputs = [s.get("output_type", "") for s in session_signals]
        output_counts = Counter(outputs)
        total = len(outputs)
        if total == 0:
            return
        perfect_signals = output_counts.get("code", 0) + output_counts.get("document", 0)
        raw_perfect = perfect_signals / total
        self._percentile.update("value_perfection", [raw_perfect])
        profile.perfection_vs_completion = self._percentile.normalize(
            "value_perfection", raw_perfect
        )

    def _analyze_innovation_vs_safety(
        self, profile: ValueProfile, git_signals, git_healthy: bool, insufficient: list
    ) -> None:
        if not git_signals or not git_healthy:
            insufficient.append("innovation_vs_safety")
            return
        types = [s.get("commit_type", "") for s in git_signals]
        type_counts = Counter(types)
        total = len(types)
        if total > 0:
            innov_signals = type_counts.get("feat", 0) + type_counts.get("other", 0)
            safe_signals = (
                type_counts.get("fix", 0) + type_counts.get("chore", 0) + type_counts.get("docs", 0)
            )
            if innov_signals + safe_signals > 0:
                raw_innov = innov_signals / (innov_signals + safe_signals)
                self._percentile.update("value_innovation", [raw_innov])
                profile.innovation_vs_safety = self._percentile.normalize(
                    "value_innovation", raw_innov
                )

        weekend_ratio = sum(1 for s in git_signals if s.get("is_weekend")) / len(git_signals)
        profile.innovation_vs_safety = (profile.innovation_vs_safety + weekend_ratio) / 2

    def _analyze_autonomy_vs_collaboration(
        self, profile: ValueProfile, git_signals, git_healthy: bool, insufficient: list
    ) -> None:
        if not git_signals or not git_healthy:
            insufficient.append("autonomy_vs_collaboration")
            return
        collaborative = sum(
            1 for s in git_signals if s.get("has_issue_reference") or s.get("has_pr_reference")
        )
        total = len(git_signals)
        if total == 0:
            return
        raw_collab = collaborative / total
        self._percentile.update("value_collaboration", [raw_collab])
        profile.autonomy_vs_collaboration = 1.0 - self._percentile.normalize(
            "value_collaboration", raw_collab
        )

    def _analyze_action_vs_analysis(
        self, profile: ValueProfile, session_signals, insufficient: list
    ) -> None:
        if not session_signals:
            insufficient.append("action_vs_analysis")
            return
        outputs = [s.get("output_type", "") for s in session_signals]
        interrupted = sum(1 for s in session_signals if s.get("termination_type") == "interrupted")
        action_signals = sum(1 for o in outputs if o in ("code", "document", "deploy"))
        analysis_signals = sum(1 for o in outputs if o in ("discussion", "analysis", "review"))
        total_out = action_signals + analysis_signals
        if total_out == 0:
            insufficient.append("action_vs_analysis")
            return
        raw_action = action_signals / total_out
        self._percentile.update("value_action", [raw_action])
        base = self._percentile.normalize("value_action", raw_action)
        if interrupted > 0:
            base = min(1.0, base + 0.2 * min(interrupted / len(session_signals), 1.0))
        profile.action_vs_analysis = base

    def _apply_value_calibration(self, profile: ValueProfile, session_signals) -> None:
        if not session_signals:
            return
        calibrations = self._calibrator.calibrate(session_signals, "value")
        for dim, adj in calibrations.items():
            if hasattr(profile, dim):
                setattr(profile, dim, self._calibrator.apply(getattr(profile, dim), adj))

    def _clamp_value_scores(self, profile: ValueProfile) -> None:
        profile.correctness_vs_efficiency = max(0.0, min(1.0, profile.correctness_vs_efficiency))
        profile.depth_vs_breadth = max(0.0, min(1.0, profile.depth_vs_breadth))
        profile.perfection_vs_completion = max(0.0, min(1.0, profile.perfection_vs_completion))
        profile.innovation_vs_safety = max(0.0, min(1.0, profile.innovation_vs_safety))
        profile.autonomy_vs_collaboration = max(0.0, min(1.0, profile.autonomy_vs_collaboration))
        profile.action_vs_analysis = max(0.0, min(1.0, profile.action_vs_analysis))

    def _analyze_value(self, session_signals, git_signals, wiki_signals) -> ValueProfile:
        """分析价值优先级（使用分位数归一化 + 行为校准）"""
        profile = ValueProfile()
        insufficient: list = []
        git_healthy = len(git_signals) >= self.MIN_SIGNALS.get("git", 5)

        self._analyze_correctness_vs_efficiency(profile, session_signals)
        self._analyze_depth_vs_breadth(profile, session_signals)
        self._analyze_perfection_vs_completion(profile, session_signals)
        self._analyze_innovation_vs_safety(profile, git_signals, git_healthy, insufficient)
        self._analyze_autonomy_vs_collaboration(profile, git_signals, git_healthy, insufficient)
        self._analyze_action_vs_analysis(profile, session_signals, insufficient)
        self._apply_value_calibration(profile, session_signals)
        self._clamp_value_scores(profile)

        profile.confidence = self._calculate_confidence(
            {
                "session": len(session_signals),
                "git": len(git_signals),
                "wiki": len(wiki_signals),
            }
        )

        profile.insufficient_dimensions = insufficient
        return profile

    # ---- 辅助方法 ----

    def _calculate_confidence(self, signal_counts: Dict[str, int]) -> float:
        """计算画像置信度。仅按实际有数据的源数做平均，避免空源拉低置信度。"""
        confidence = 0.0
        active_sources = 0
        for source, count in signal_counts.items():
            min_required = self.MIN_SIGNALS.get(source, 10)
            source_confidence = min(1.0, count / min_required)
            confidence += source_confidence
            if count > 0:
                active_sources += 1

        return min(1.0, confidence / max(active_sources, 1))

    @staticmethod
    def _merge_domain_preferences(
        previous: Dict[str, float],
        current: Dict[str, float],
    ) -> Dict[str, float]:
        """Merge domain preference scores while preserving prior domains."""
        merged = dict(previous)
        for domain, score in current.items():
            merged[domain] = max(score, merged.get(domain, 0.0))
        return merged

    def _calculate_changes(
        self,
        energy: EnergyProfile,
        cognitive: CognitiveProfile,
        value: ValueProfile,
        previous: PreferenceProfile,
    ):
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
            "startup_difficulty": calc_change(
                energy.startup_difficulty, previous.energy.startup_difficulty
            ),
            "endurance_mode": calc_change(energy.endurance_mode, previous.energy.endurance_mode),
            "switching_flexibility": calc_change(
                energy.switching_flexibility, previous.energy.switching_flexibility
            ),
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
            "correctness_vs_efficiency": calc_change(
                value.correctness_vs_efficiency, previous.value.correctness_vs_efficiency
            ),
            "depth_vs_breadth": calc_change(
                value.depth_vs_breadth, previous.value.depth_vs_breadth
            ),
            "perfection_vs_completion": calc_change(
                value.perfection_vs_completion, previous.value.perfection_vs_completion
            ),
            "innovation_vs_safety": calc_change(
                value.innovation_vs_safety, previous.value.innovation_vs_safety
            ),
            "autonomy_vs_collaboration": calc_change(
                value.autonomy_vs_collaboration, previous.value.autonomy_vs_collaboration
            ),
            "action_vs_analysis": calc_change(
                value.action_vs_analysis, previous.value.action_vs_analysis
            ),
        }

    def detect_drift(
        self, current: PreferenceProfile, previous: PreferenceProfile | None = None
    ) -> List[Dict]:
        """
        检测画像漂移。

        漂移类型：
        1. sudden_shift: 单维度变化 > 0.25，可能是噪声或重大生活变化
        2. gradual_drift: 多维度同向缓慢偏移 > 0.15，偏好确实在演化
        3. update_lag: 画像版本过旧（> {cls.UPDATE_LAG_DAYS_THRESHOLD}天未更新）
        4. low_confidence_drift: 高变化 + 低置信度 = 数据不足，不应过度解读

        Returns:
            漂移警报列表
        """
        alerts = []  # type: ignore[var-annotated]

        if not previous:
            return alerts

        # 检查所有维度的变化
        dimensions = [
            ("energy.focus_depth", previous.energy.focus_depth, current.energy.focus_depth),
            (
                "energy.startup_difficulty",
                previous.energy.startup_difficulty,
                current.energy.startup_difficulty,
            ),
            (
                "energy.endurance_mode",
                previous.energy.endurance_mode,
                current.energy.endurance_mode,
            ),
            (
                "energy.switching_flexibility",
                previous.energy.switching_flexibility,
                current.energy.switching_flexibility,
            ),
            (
                "energy.recovery_cycle",
                previous.energy.recovery_cycle,
                current.energy.recovery_cycle,
            ),
            (
                "cognitive.abstraction",
                previous.cognitive.abstraction,
                current.cognitive.abstraction,
            ),
            (
                "cognitive.system_view",
                previous.cognitive.system_view,
                current.cognitive.system_view,
            ),
            ("cognitive.skepticism", previous.cognitive.skepticism, current.cognitive.skepticism),
            ("cognitive.creativity", previous.cognitive.creativity, current.cognitive.creativity),
            ("cognitive.deduction", previous.cognitive.deduction, current.cognitive.deduction),
            (
                "value.correctness_vs_efficiency",
                previous.value.correctness_vs_efficiency,
                current.value.correctness_vs_efficiency,
            ),
            (
                "value.depth_vs_breadth",
                previous.value.depth_vs_breadth,
                current.value.depth_vs_breadth,
            ),
            (
                "value.perfection_vs_completion",
                previous.value.perfection_vs_completion,
                current.value.perfection_vs_completion,
            ),
            (
                "value.innovation_vs_safety",
                previous.value.innovation_vs_safety,
                current.value.innovation_vs_safety,
            ),
            (
                "value.autonomy_vs_collaboration",
                previous.value.autonomy_vs_collaboration,
                current.value.autonomy_vs_collaboration,
            ),
            (
                "value.action_vs_analysis",
                previous.value.action_vs_analysis,
                current.value.action_vs_analysis,
            ),
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
            avg_confidence = (
                current.energy.confidence + current.cognitive.confidence + current.value.confidence
            ) / 3
            if avg_confidence < 0.4:
                alert_type = "low_confidence_drift"
                advice = "画像置信度低，此次变化可能是数据不足导致的噪声，建议继续观察"
            else:
                alert_type = "sudden_shift"
                advice = "单维度发生剧烈变化，可能反映了重大情境变化（如换工作、新项目），建议审视是否为持久变化"

            alerts.append(
                {
                    "type": alert_type,
                    "dimension": name,
                    "previous": round(prev, 2),
                    "current": round(curr, 2),
                    "delta": round(delta, 2),
                    "severity": "high" if abs(delta) > 0.35 else "medium",
                    "advice": advice,
                }
            )

        # 类型2： gradual_drift（多维度同向偏移）
        if len(gradual_drifts) >= 3:
            # 检查是否同向
            positive = sum(1 for _, _, _, d in gradual_drifts if d > 0)
            negative = sum(1 for _, _, _, d in gradual_drifts if d < 0)
            if positive >= 3 or negative >= 3:
                direction = "上升" if positive > negative else "下降"
                alerts.append(
                    {
                        "type": "gradual_drift",
                        "dimension": f"{len(gradual_drifts)}个维度同向{direction}",
                        "previous": None,
                        "current": None,
                        "delta": None,
                        "severity": "medium",
                        "advice": f"多个维度同时{direction}，偏好正在演化。建议在下个季度关注这些变化是否持续",
                    }
                )

        # 类型3： update_lag
        try:
            prev_date = datetime.fromisoformat(previous.generated_at.replace("Z", "+00:00"))
            days_since = (datetime.now() - prev_date).days
            if days_since > self.UPDATE_LAG_DAYS_THRESHOLD:
                alerts.append(
                    {
                        "type": "update_lag",
                        "dimension": "画像更新",
                        "previous": f"{days_since}天前",
                        "current": "现在",
                        "delta": days_since,
                        "severity": "medium",
                        "advice": f"画像已超过{self.UPDATE_LAG_DAYS_THRESHOLD}天未更新，可能无法反映当前偏好，建议尽快重新分析",
                    }
                )
        except (OSError, ValueError, TypeError) as e:
            logger.warning("忽略异常: %s", e, exc_info=True)

        return alerts

    def _parse_intervals(self, timestamps: List[str]) -> List[float]:
        """解析时间戳间隔（秒）"""
        intervals = []
        try:
            parsed = []
            for ts in timestamps:
                if not ts or not ts.strip():
                    continue
                try:
                    dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
                    parsed.append(dt)
                except (ValueError, TypeError):
                    logger.debug("跳过无法解析的时间戳: %s", ts)
                    continue

            parsed.sort()
            for i in range(1, len(parsed)):
                delta = (parsed[i] - parsed[i - 1]).total_seconds()
                if (
                    self.INTERVAL_FILTER_MIN_SECONDS < delta < self.INTERVAL_FILTER_MAX_SECONDS
                ):  # 过滤异常值
                    intervals.append(delta)
        except (OSError, ValueError, TypeError) as e:
            logger.warning("忽略异常: %s", e, exc_info=True)
        return intervals

    def _get_git_signals(self, days: int) -> List[Dict]:
        """获取Git信号（复用连接池）"""
        try:
            conn = self.store._pool.get_conn()
            conn.row_factory = sqlite3.Row  # noqa
            cursor = conn.execute(
                """
                SELECT * FROM git_signals
                WHERE timestamp >= date('now', ?)
                ORDER BY timestamp DESC
            """,
                (f"-{days} days",),
            )
            return [dict(row) for row in cursor.fetchall()]
        except (OSError, ValueError, sqlite3.Error):
            logger.warning("Caught unexpected error at pythia.py", exc_info=True)
            return []

    def _get_wiki_signals(self, days: int) -> List[Dict]:
        """获取Wiki信号（复用连接池）"""
        try:
            conn = self.store._pool.get_conn()
            conn.row_factory = sqlite3.Row  # noqa
            cursor = conn.execute(
                """
                SELECT * FROM knowledge_signals
                WHERE timestamp >= date('now', ?)
                ORDER BY timestamp DESC
            """,
                (f"-{days} days",),
            )
            return [dict(row) for row in cursor.fetchall()]
        except (OSError, ValueError, sqlite3.Error):
            logger.warning("Caught unexpected error at pythia.py", exc_info=True)
            return []

    def _get_reflection_signals(self, days: int) -> List[Dict]:
        """获取最近N天的 Layer 5 反射信号。"""
        try:
            if hasattr(self.store, "get_recent_reflection_signals"):
                return self.store.get_recent_reflection_signals(days=days)
        except (
            OSError,
            ValueError,
            TypeError,
            KeyError,
            ImportError,
            AttributeError,
            RuntimeError,
            sqlite3.Error,
        ):
            logger.warning("[PreferenceAnalyzer] 读取 reflection 信号失败", exc_info=True)
        return []

    def _get_reflection_signals_since(self, since_iso: str) -> List[Dict]:
        """获取自指定时间以来的 Layer 5 反射信号（用于增量分析）。"""
        try:
            if not since_iso:
                return []
            if hasattr(self.store, "get_reflection_signals_since"):
                return self.store.get_reflection_signals_since(since_iso)
        except (
            OSError,
            ValueError,
            TypeError,
            KeyError,
            ImportError,
            AttributeError,
            RuntimeError,
            sqlite3.Error,
        ):
            logger.warning("[PreferenceAnalyzer] 读取增量 reflection 信号失败", exc_info=True)
        return []

    _REFLECTION_DIMENSION_MAP = {
        "reflection_interest": {
            "coding": ("abstraction", 0.05),
            "design": ("creativity", 0.05),
            "product": ("system_view", 0.05),
            "ops": ("system_view", 0.05),
        },
        "cognitive_shift": {
            "abstract": ("abstraction", 0.08),
            "concrete": ("abstraction", -0.08),
            "system": ("system_view", 0.08),
            "detail": ("system_view", -0.08),
            "skeptical": ("skepticism", 0.08),
            "creative": ("creativity", 0.08),
            "optimize": ("creativity", -0.08),
        },
        "value_priority": {
            "correctness": ("correctness_vs_efficiency", 0.08),
            "efficiency": ("correctness_vs_efficiency", -0.08),
            "depth": ("depth_vs_breadth", 0.08),
            "breadth": ("depth_vs_breadth", -0.08),
            "perfect": ("perfection_vs_completion", 0.08),
            "complete": ("perfection_vs_completion", -0.08),
            "innovation": ("innovation_vs_safety", 0.08),
            "safe": ("innovation_vs_safety", -0.08),
            "autonomy": ("autonomy_vs_collaboration", 0.08),
            "collaboration": ("autonomy_vs_collaboration", -0.08),
            "action": ("action_vs_analysis", 0.08),
            "analysis": ("action_vs_analysis", -0.08),
        },
    }

    def _apply_reflection_signals(
        self,
        cognitive: CognitiveProfile,
        value: ValueProfile,
        signals: List[Dict],
    ) -> Tuple[CognitiveProfile, ValueProfile]:
        """将 Layer 5 反射信号作为专家标注微调认知/价值维度。"""
        if not signals:
            return cognitive, value

        adjustments: Dict[str, float] = defaultdict(float)
        for s in signals:
            dimension = (s.get("dimension") or "").lower()
            value_text = (s.get("value") or "").lower()
            confidence = float(s.get("confidence") or 0.5)
            mapping = self._REFLECTION_DIMENSION_MAP.get(dimension, {})
            for keyword, (attr, delta) in mapping.items():
                if keyword in value_text:
                    adjustments[attr] += delta * confidence

        if not adjustments:
            return cognitive, value

        for attr, adj in adjustments.items():
            if hasattr(cognitive, attr):
                current = getattr(cognitive, attr)
                setattr(cognitive, attr, max(0.0, min(1.0, current + adj)))
            elif hasattr(value, attr):
                current = getattr(value, attr)
                setattr(value, attr, max(0.0, min(1.0, current + adj)))

        return cognitive, value

    def _get_fs_signals(self, days: int) -> List[Dict]:
        """获取文件系统信号（复用连接池）"""
        try:
            conn = self.store._pool.get_conn()
            conn.row_factory = sqlite3.Row  # noqa
            cursor = conn.execute(
                """
                SELECT * FROM file_system_signals
                WHERE timestamp >= date('now', ?)
                    ORDER BY timestamp DESC
                """,
                (f"-{days} days",),
            )
            return [dict(row) for row in cursor.fetchall()]
        except (OSError, ValueError, sqlite3.Error, AttributeError):
            logger.warning("Caught unexpected error at pythia.py", exc_info=True)
            return []


# ========== 便捷函数 ==========


def analyze_preferences(
    days: int = ANALYZE_PREFERENCES_PREFERENCE_PROFILE_DAYS,
) -> PreferenceProfile:
    """便捷函数：分析偏好画像"""
    analyzer = PreferenceAnalyzer()
    return analyzer.analyze(days=days)


def generate_radar_report(profile: PreferenceProfile | None = None) -> str:
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
