"""Typed preference profiles and reusable calibration helpers."""

import logging
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Dict, List, Optional

PREFERENCE_ANALYZER_STARTUP_DIFFICULTY_SCALE_SECONDS = 3600
PREFERENCE_ANALYZER_INTERVAL_FILTER_MAX_SECONDS = 86400
PREFERENCE_ANALYZER_INTERVAL_FILTER_MAX_SECONDS_2 = 30
PREFERENCE_ANALYZER_ANALYZE_PREFERENCE_PROFILE_DAYS = 90
PREFERENCE_ANALYZER__FALLBACK_FROM_KNOWLEDGE_PROFILE_PREFERENCE_PROFILE_DAYS = 90
ANALYZE_PREFERENCES_PREFERENCE_PROFILE_DAYS = 90

logger = logging.getLogger(__name__)


@dataclass
class EnergyProfile:
    """Layer 1: 能量模式 - 你的能量怎么流动"""

    focus_depth: float = 0.5  # 专注深度 0=碎片化, 1=深度沉浸
    startup_difficulty: float = 0.5  # 启动难度 0=一触即发, 1=需要推力
    endurance_mode: float = 0.5  # 续航模式 0=爆发型, 1=匀速型
    switching_flexibility: float = 0.5  # 切换弹性 0=单线程, 1=多线程
    recovery_cycle: float = 0.5  # 恢复周期 0=快速恢复, 1=需要缓冲
    confidence: float = 0.0
    insufficient_dimensions: Optional[List[str]] = None  # 数据不足的维度列表


@dataclass
class CognitiveProfile:
    """Layer 2: 认知模式 - 你的大脑默认怎么运转"""

    abstraction: float = 0.5  # 抽象↔具象 0=从案例归纳, 1=从原理推导
    system_view: float = 0.5  # 系统↔单点 0=聚焦当前, 1=先看全局
    skepticism: float = 0.5  # 质疑↔信任 0=信任框架, 1=挑战前提
    creativity: float = 0.5  # 创造↔优化 0=从1到N, 1=从0到1
    deduction: float = 0.5  # 演绎↔归纳 0=从经验总结, 1=从规则推导
    confidence: float = 0.0
    insufficient_dimensions: List[str] = None  # type: ignore[assignment]


@dataclass
class ValueProfile:
    """Layer 3: 价值优先级 - 你做选择时的底层权重"""

    correctness_vs_efficiency: float = 0.5  # 正确性↔效率
    depth_vs_breadth: float = 0.5  # 深度↔广度
    perfection_vs_completion: float = 0.5  # 完美↔完成
    innovation_vs_safety: float = 0.5  # 创新↔稳妥
    autonomy_vs_collaboration: float = 0.5  # 自主↔协作
    action_vs_analysis: float = 0.5  # 行动↔分析 0=分析优先, 1=行动优先
    confidence: float = 0.0
    insufficient_dimensions: List[str] = None  # type: ignore[assignment]


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
    domain_preferences: Dict[str, float] = field(default_factory=dict)
    user_confirmed: bool = False
    confirmed_at: str = ""
    calibration_score: Optional[float] = None
    source_signal_ids: Dict[str, List[int]] = field(
        default_factory=dict,
        repr=False,
        compare=False,
    )

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
            "domain_preferences": dict(self.domain_preferences),
            "user_confirmed": self.user_confirmed,
            "confirmed_at": self.confirmed_at,
            "calibration_score": self.calibration_score,
        }

    def _energy_to_dict(self) -> Dict:
        ins = set(self.energy.insufficient_dimensions or [])
        return {
            "focus_depth": {
                "score": round(self.energy.focus_depth, 2),
                "label": self._label_depth(self.energy.focus_depth),
            },
            "startup_difficulty": {
                "score": round(self.energy.startup_difficulty, 2),
                "label": self._label_startup(self.energy.startup_difficulty),
            },
            "endurance_mode": {
                "score": round(self.energy.endurance_mode, 2),
                "label": (
                    "爆发型"
                    if self.energy.endurance_mode < 0.4
                    else "匀速型" if self.energy.endurance_mode > 0.6 else "混合型"
                ),
            },
            "switching_flexibility": {
                "score": round(self.energy.switching_flexibility, 2),
                "label": (
                    "单线程"
                    if self.energy.switching_flexibility < 0.4
                    else "多线程" if self.energy.switching_flexibility > 0.6 else "弹性切换"
                ),
            },
            "recovery_cycle": (
                {"score": "—", "label": "数据不足"}
                if "recovery_cycle" in ins
                else {
                    "score": round(self.energy.recovery_cycle, 2),
                    "label": (
                        "快速恢复"
                        if self.energy.recovery_cycle < 0.4
                        else "需要缓冲" if self.energy.recovery_cycle > 0.6 else "中等恢复"
                    ),
                }
            ),
            "confidence": round(self.energy.confidence, 2),
        }

    def _cognitive_to_dict(self) -> Dict:
        ins = set(self.cognitive.insufficient_dimensions or [])
        return {
            "abstraction": {
                "score": round(self.cognitive.abstraction, 2),
                "label": (
                    "具象型"
                    if self.cognitive.abstraction < 0.4
                    else "抽象型" if self.cognitive.abstraction > 0.6 else "平衡型"
                ),
            },
            "system_view": {
                "score": round(self.cognitive.system_view, 2),
                "label": (
                    "单点聚焦"
                    if self.cognitive.system_view < 0.4
                    else "系统视角" if self.cognitive.system_view > 0.6 else "视情况"
                ),
            },
            "skepticism": {
                "score": round(self.cognitive.skepticism, 2),
                "label": (
                    "信任框架"
                    if self.cognitive.skepticism < 0.4
                    else "质疑前提" if self.cognitive.skepticism > 0.6 else "适度质疑"
                ),
            },
            "creativity": (
                {"score": "—", "label": "数据不足"}
                if "creativity" in ins
                else {
                    "score": round(self.cognitive.creativity, 2),
                    "label": (
                        "优化型"
                        if self.cognitive.creativity < 0.4
                        else "创造型" if self.cognitive.creativity > 0.6 else "两者兼顾"
                    ),
                }
            ),
            "deduction": (
                {"score": "—", "label": "数据不足"}
                if "deduction" in ins
                else {
                    "score": round(self.cognitive.deduction, 2),
                    "label": (
                        "归纳型"
                        if self.cognitive.deduction < 0.4
                        else "演绎型" if self.cognitive.deduction > 0.6 else "混合使用"
                    ),
                }
            ),
            "confidence": round(self.cognitive.confidence, 2),
        }

    def _value_to_dict(self) -> Dict:
        ins = set(self.value.insufficient_dimensions or [])
        return {
            "correctness_vs_efficiency": {
                "score": round(self.value.correctness_vs_efficiency, 2),
                "label": (
                    "效率优先"
                    if self.value.correctness_vs_efficiency < 0.4
                    else (
                        "正确性优先" if self.value.correctness_vs_efficiency > 0.6 else "视情况平衡"
                    )
                ),
            },
            "depth_vs_breadth": {
                "score": round(self.value.depth_vs_breadth, 2),
                "label": (
                    "广度优先"
                    if self.value.depth_vs_breadth < 0.4
                    else "深度优先" if self.value.depth_vs_breadth > 0.6 else "两者兼顾"
                ),
            },
            "perfection_vs_completion": {
                "score": round(self.value.perfection_vs_completion, 2),
                "label": (
                    "先完成"
                    if self.value.perfection_vs_completion < 0.4
                    else "先完美" if self.value.perfection_vs_completion > 0.6 else "平衡"
                ),
            },
            "innovation_vs_safety": (
                {"score": "—", "label": "数据不足"}
                if "innovation_vs_safety" in ins
                else {
                    "score": round(self.value.innovation_vs_safety, 2),
                    "label": (
                        "稳妥优先"
                        if self.value.innovation_vs_safety < 0.4
                        else "创新优先" if self.value.innovation_vs_safety > 0.6 else "视风险而定"
                    ),
                }
            ),
            "autonomy_vs_collaboration": (
                {"score": "—", "label": "数据不足"}
                if "autonomy_vs_collaboration" in ins
                else {
                    "score": round(self.value.autonomy_vs_collaboration, 2),
                    "label": (
                        "协作优先"
                        if self.value.autonomy_vs_collaboration < 0.4
                        else (
                            "自主优先" if self.value.autonomy_vs_collaboration > 0.6 else "灵活切换"
                        )
                    ),
                }
            ),
            "action_vs_analysis": (
                {"score": "—", "label": "数据不足"}
                if "action_vs_analysis" in ins
                else {
                    "score": round(self.value.action_vs_analysis, 2),
                    "label": (
                        "分析优先"
                        if self.value.action_vs_analysis < 0.4
                        else "行动优先" if self.value.action_vs_analysis > 0.6 else "视情况平衡"
                    ),
                }
            ),
            "confidence": round(self.value.confidence, 2),
        }

    @staticmethod
    def _label_depth(score: float) -> str:
        if score < 0.3:
            return "碎片化"
        if score < 0.5:
            return "中等专注"
        if score < 0.7:
            return "较深度"
        return "深度沉浸"

    @staticmethod
    def _label_startup(score: float) -> str:
        if score < 0.3:
            return "一触即发"
        if score < 0.5:
            return "启动较快"
        if score < 0.7:
            return "需要准备"
        return "需要推力"


class _PercentileNormalizer:
    """滑动窗口分位数归一化器。

    将原始指标在滑动窗口内做 percentile 转换，消除绝对阈值依赖。
    例如：原始纠正次数从 "3次→质疑倾向0.5" 变为 "在同龄用户中排前20%→质疑倾向0.8"。
    """

    def __init__(self, window_size: int = 100):
        self.window_size = window_size
        self._windows: Dict[str, List[float]] = defaultdict(list)

    def update(self, dimension: str, values: List[float]) -> None:
        """向指定维度的窗口追加原始值。"""
        self._windows[dimension].extend(values)
        # 保持窗口大小
        if len(self._windows[dimension]) > self.window_size:
            self._windows[dimension] = self._windows[dimension][-self.window_size :]

    def normalize(self, dimension: str, value: float) -> float:
        """将单个原始值转换为 [0, 1] 分位数分数。

        Returns:
            0.0 = 窗口内最小值，0.5 = 中位数，1.0 = 最大值
            窗口为空时返回 0.5（中性默认值）
        """
        window = self._windows.get(dimension, [])
        if not window:
            return 0.5

        # 使用简单的 rank-based percentile
        sorted_vals = sorted(window)
        n = len(sorted_vals)

        # 找到 value 在排序后的位置（线性插值）
        if value < sorted_vals[0]:
            return 0.0
        if value > sorted_vals[-1]:
            return 1.0
        if n == 1:
            return 0.5

        # 找到相邻两个点并插值
        for i in range(n - 1):
            if sorted_vals[i] <= value <= sorted_vals[i + 1]:
                lower_rank = i / (n - 1) if n > 1 else 0.5
                upper_rank = (i + 1) / (n - 1) if n > 1 else 0.5
                if sorted_vals[i + 1] == sorted_vals[i]:
                    return lower_rank
                ratio = (value - sorted_vals[i]) / (sorted_vals[i + 1] - sorted_vals[i])
                return lower_rank + ratio * (upper_rank - lower_rank)

        return 0.5

    def batch_normalize(self, dimension: str, values: List[float]) -> List[float]:
        """批量归一化，先更新窗口再计算分位数。"""
        self.update(dimension, values)
        return [self.normalize(dimension, v) for v in values]

    def get_window_stats(self, dimension: str) -> Dict[str, float]:
        """获取窗口统计信息（用于调试）。"""
        window = self._windows.get(dimension, [])
        if not window:
            return {}
        sorted_w = sorted(window)
        n = len(sorted_w)
        return {
            "count": n,
            "min": sorted_w[0],
            "p25": sorted_w[n // 4],
            "median": sorted_w[n // 2],
            "p75": sorted_w[3 * n // 4],
            "max": sorted_w[-1],
        }


class _BehaviorCalibrator:
    """行为推断校准器。

    通过观察用户实际行为自动修正画像，不弹出任何 UI。
    核心规则：
    - 用户纠正 AI 回答 → 自动上调"质疑倾向"
    - 用户追问深度 > 5 轮 → 自动上调"专注深度"
    - 用户跳过某类建议 3 次以上 → 自动下调该类建议权重
    """

    # 校准规则：(行为模式, 目标维度, 维度层, 调整量, 最大上限)
    CALIBRATION_RULES = [
        # 能量层
        ("correction_count", "skepticism", "cognitive", 0.05, 0.9),
        ("follow_up_depth", "focus_depth", "energy", 0.03, 0.9),
        ("interrupted", "action_vs_analysis", "value", 0.05, 0.9),
        # 认知层
        ("deep_reasoning", "abstraction", "cognitive", 0.04, 0.9),
        ("rejection", "skepticism", "cognitive", 0.06, 0.95),
        # 价值层
        ("skip_suggestion", "autonomy_vs_collaboration", "value", 0.04, 0.9),
    ]

    def calibrate(self, signals: List[Dict], layer: str) -> Dict[str, float]:
        """根据信号计算校准调整量。

        Args:
            signals: 原始信号列表
            layer: 目标层 (energy/cognitive/value)

        Returns:
            {dimension: adjustment_value}，adjustment ∈ [-0.15, 0.15]
        """
        adjustments: Dict[str, float] = defaultdict(float)

        if not signals:
            return dict(adjustments)

        # 统计行为模式
        corrections = [s.get("correction_count", 0) for s in signals]
        avg_correction = sum(corrections) / len(corrections) if corrections else 0

        follow_ups = [s.get("follow_up_depth", 0) for s in signals]
        avg_followup = sum(follow_ups) / len(follow_ups) if follow_ups else 0

        interrupted = sum(1 for s in signals if s.get("termination_type") == "interrupted")
        interrupted_rate = interrupted / len(signals) if signals else 0

        # 应用校准规则。CALIBRATION_RULES 是信号模式到画像维度的契约，
        # 具体公式仍由各 pattern 的 adjuster 负责。
        for pattern, dimension, rule_layer, step, _max_score in self.CALIBRATION_RULES:
            if layer and rule_layer != layer:
                continue
            adjustment = self._adjust_for_rule(
                pattern,
                signals,
                avg_correction=avg_correction,
                avg_followup=avg_followup,
                interrupted_rate=interrupted_rate,
                step=step,
            )
            if adjustment:
                adjustments[dimension] += adjustment

        # 限制总调整量，防止剧烈抖动
        for dim in adjustments:
            adjustments[dim] = max(-0.15, min(0.15, adjustments[dim]))

        return dict(adjustments)

    def _adjust_for_rule(
        self,
        pattern: str,
        signals: List[Dict],
        *,
        avg_correction: float,
        avg_followup: float,
        interrupted_rate: float,
        step: float,
    ) -> float:
        if pattern == "correction_count":
            return self._adjust_for_corrections(avg_correction)
        if pattern == "follow_up_depth":
            return self._adjust_for_followup(avg_followup)
        if pattern == "interrupted":
            return self._adjust_for_interruptions(interrupted_rate)
        if pattern == "deep_reasoning":
            return self._adjust_for_deep_reasoning(signals)
        return self._adjust_for_signal_count(signals, pattern, step)

    @staticmethod
    def _adjust_for_signal_count(signals: List[Dict], pattern: str, step: float) -> float:
        """通用布尔/计数字段校准，用于新增规则无需再改主流程。"""
        if not signals:
            return 0.0
        total = 0.0
        for signal in signals:
            value = signal.get(pattern, 0)
            if isinstance(value, bool):
                total += 1.0 if value else 0.0
            elif isinstance(value, (int, float)):
                total += max(0.0, float(value))
            elif value:
                total += 1.0
        average_signal = total / len(signals)
        if average_signal <= 0:
            return 0.0
        return min(0.1, average_signal * step)

    @staticmethod
    def _adjust_for_corrections(avg_correction: float) -> float:
        """纠正 → 质疑倾向上升。"""
        adjustment = 0.0
        if avg_correction >= 1.0:
            adjustment += min(0.1, avg_correction * 0.03)
        if avg_correction >= 3.0:
            adjustment += 0.05  # 连续大量纠正，更强烈的信号
        return adjustment

    @staticmethod
    def _adjust_for_followup(avg_followup: float) -> float:
        """追问深度 → 专注深度上升。"""
        adjustment = 0.0
        if avg_followup >= 3.0:
            adjustment += min(0.1, (avg_followup - 2) * 0.02)
        if avg_followup >= 5.0:
            adjustment += 0.03
        return adjustment

    @staticmethod
    def _adjust_for_interruptions(interrupted_rate: float) -> float:
        """打断 → 行动偏好上升（分析过多用户不耐烦）。"""
        if interrupted_rate >= 0.2:
            return min(0.1, interrupted_rate * 0.2)
        return 0.0

    @staticmethod
    def _adjust_for_deep_reasoning(signals: List[Dict]) -> float:
        """深度推理词汇 → 抽象能力上升。"""
        abstract_keywords = [
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
        ]
        deep_reasoning_count = 0
        for s in signals:
            content = (s.get("final_feedback") or "") + " " + (s.get("selection_rationale") or "")
            deep_reasoning_count += sum(1 for kw in abstract_keywords if kw in content)
        if deep_reasoning_count >= 3:
            return min(0.1, deep_reasoning_count * 0.01)
        return 0.0

    def apply(self, base_score: float, adjustment: float) -> float:
        """将调整量应用到基础分数上，确保结果在 [0, 1] 范围内。"""
        return max(0.0, min(1.0, base_score + adjustment))


class _DomainPreferenceAnalyzer:
    """Domain 偏好综合加权分析器。

    综合三种信号源的加权得分：
    - 对话主动提及（权重 0.4）
    - 主动搜索（权重 0.3）
    - wiki 编辑（权重 0.3）

    综合得分 ≥ 1.0 时记录为该 domain 偏好。
    """

    WEIGHTS = {
        "conversation_mention": 0.4,
        "active_search": 0.3,
        "wiki_edit": 0.3,
    }

    # 各信号源的归一化分母（7天窗口内的阈值）
    NORMALIZERS = {
        "conversation_mention": 3.0,  # 7天内提及3次 = 满分
        "active_search": 2.0,  # 7天内搜索2次 = 满分
        "wiki_edit": 1.0,  # 7天内编辑1次 = 满分
    }

    def analyze(self, session_signals: List[Dict], wiki_signals: List[Dict]) -> Dict[str, float]:
        """分析 domain 偏好，返回 {domain: preference_score}。"""
        domain_scores: Dict[str, Dict[str, float]] = defaultdict(lambda: defaultdict(float))

        # 1. 对话提及（从 task_type 提取 domain）
        for s in session_signals:
            task_type = s.get("task_type", "")
            if task_type:
                domain = task_type.split("/")[0] if "/" in task_type else task_type
                domain_scores[domain]["conversation_mention"] += 1

        # 2. wiki 编辑（从 page_path 推断 domain）
        for s in wiki_signals:
            action = s.get("action_type", "")
            if action in ("modify", "create"):
                page = s.get("page_path", "")
                # 从路径推断 domain（如 03-Tech/python.md → tech）
                domain = self._infer_domain_from_path(page)
                domain_scores[domain]["wiki_edit"] += 1

        # 3. 综合加权计算
        results = {}
        for domain, counts in domain_scores.items():
            total = 0.0
            for source, weight in self.WEIGHTS.items():
                raw_count = counts.get(source, 0)
                normalized = min(1.0, raw_count / self.NORMALIZERS[source])
                total += normalized * weight
            results[domain] = round(total, 3)

        return results

    def _infer_domain_from_path(self, path: str) -> str:
        """从 wiki 页面路径推断 domain。"""
        path_lower = path.lower()
        if "03-tech" in path_lower or "tech" in path_lower:
            return "tech"
        if "02-project" in path_lower or "project" in path_lower:
            return "project"
        if "01-people" in path_lower or "people" in path_lower:
            return "people"
        if "04-concept" in path_lower or "concept" in path_lower:
            return "concept"
        if "05-moc" in path_lower or "moc" in path_lower:
            return "moc"
        # 从文件名推断
        filename = path.split("/")[-1].lower() if "/" in path else path_lower
        for keyword, domain in [
            ("python", "tech"),
            ("js", "tech"),
            ("code", "tech"),
            ("design", "design"),
            ("product", "product"),
            ("marketing", "marketing"),
            ("ops", "ops"),
        ]:
            if keyword in filename:
                return domain
        return "general"
