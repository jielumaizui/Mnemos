import logging

"""
Trigger Detector — 决策触发检测

核心职责：
检测用户是否进入了需要 Reflection 的决策情境。

检测策略（按优先级）：
1. **Observation 突变检测**（主动）：从 Observation 变化中检测行为信号
2. **时间节律触发**（主动）：年初、季度末等关键节点主动触发
3. **关键词匹配**（兜底）：用户明确说出决策相关词汇时触发

设计原则：
- **主动检测 > 被动等待**：系统从行为中感知，不等用户说出口
- **行为信号 > 语言信号**：Observation 突变比关键词更可靠
- **宁可多触，不可漏触**：误触可以忽略，漏触损失认知记录
- **代码层实现**：所有检测逻辑在代码层完成，不依赖 Agent 推理
"""

import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Dict, List, Optional

from core.cognitive.models import Dimension, Observation
from core.cognitive.observation_store import ObservationStore as ObsStore
from core.reflection.models import ReflectionTrigger

logger = logging.getLogger(__name__)


# 触发信号配置（关键词兜底）
TRIGGER_SIGNALS = [
    (
        ReflectionTrigger.NEW_PROJECT,
        [
            (r"启动\s*.*项目", 3.0),
            (r"新建\s*.*项目", 3.0),
            (r"开始\s*做\s*.*", 2.0),
            (r"立项", 3.0),
            (r"孵化\s*.*", 2.0),
            (r"创建\s*.*项目", 3.0),
            (r"开\s*.*新\s*.*坑", 2.5),
            (r"准备\s*.*开发", 2.0),
            (r"打算\s*.*做", 1.5),
            (r"计划\s*.*启动", 2.0),
        ],
        2.5,
    ),
    (
        ReflectionTrigger.ABANDON_PROJECT,
        [
            (r"放弃\s*.*项目", 3.0),
            (r"停止\s*.*项目", 3.0),
            (r"终止\s*.*", 2.5),
            (r"不\s*.*做\s*.*了", 2.0),
            (r"砍掉\s*.*", 2.5),
            (r"搁置\s*.*", 2.0),
            (r"暂停\s*.*项目", 2.0),
            (r"解散\s*.*", 2.5),
            (r"退出\s*.*", 2.0),
            (r"失败\s*.*", 1.5),
        ],
        2.5,
    ),
    (
        ReflectionTrigger.LONG_TERM_PLAN,
        [
            (r"长期\s*.*规划", 3.0),
            (r"未来\s*.*年", 2.5),
            (r"三年\s*.*计划", 3.0),
            (r"五年\s*.*规划", 3.0),
            (r"战略\s*.*", 2.5),
            (r"路线图", 2.0),
            (r"roadmap", 2.0),
            (r"愿景", 2.0),
            (r"使命", 2.0),
            (r"人生\s*.*规划", 3.0),
            (r"职业\s*.*规划", 2.5),
        ],
        2.5,
    ),
    (
        ReflectionTrigger.MAJOR_DECISION,
        [
            (r"重大\s*.*决定", 3.0),
            (r"关键\s*.*选择", 3.0),
            (r"要不要\s*.*", 2.0),
            (r"是否\s*.*", 1.5),
            (r"权衡\s*.*", 2.0),
            (r"纠结\s*.*", 1.5),
            (r"难以\s*.*决定", 2.0),
            (r"投入\s*.*万", 2.5),
            (r"预算\s*.*", 1.5),
            (r"all\s*in", 2.0),
            (r"跳槽", 2.5),
            (r"转型", 2.5),
            (r"辞职", 2.5),
            (r"创业", 3.0),
        ],
        2.5,
    ),
    (
        ReflectionTrigger.ROLE_SHIFT,
        [
            (r"升职", 2.5),
            (r"晋升", 2.5),
            (r"转岗", 2.5),
            (r"调岗", 2.0),
            (r"换\s*.*方向", 2.0),
            (r"带\s*.*团队", 2.0),
            (r"管理\s*.*团队", 2.0),
            (r"成为\s*.*负责人", 2.5),
            (r"title\s*.*", 1.5),
            (r"角色\s*.*变化", 2.5),
        ],
        2.0,
    ),
    (
        ReflectionTrigger.RELATIONSHIP_CHANGE,
        [
            (r"分手", 2.5),
            (r"离婚", 3.0),
            (r"结婚", 3.0),
            (r"恋爱", 2.0),
            (r"合作\s*.*破裂", 2.5),
            (r"合伙人\s*.*", 2.0),
            (r" cofounder", 2.0),
            (r" mentor", 1.5),
            (r"导师", 1.5),
            (r"离职\s*.*同事", 1.5),
        ],
        2.5,
    ),
]

# Observation 突变检测配置
# 维度 -> (检测窗口天数, 对比窗口天数, 最小变化分数)
MUTATION_CONFIG = {
    "growth": (7, 30, 0.4),  # 成长维度：近7天 vs 前30天，变化>0.4触发
    "attention": (7, 30, 0.5),  # 关注维度：关注重心突变
    "stress": (3, 14, 0.6),  # 压力维度：近3天压力激增（更快检测）
    "decisions": (7, 30, 0.4),  # 决策维度：决策模式突变
    "actions": (7, 14, 0.5),  # 行动维度：行动力突变
}

# 时间节律触发配置
RHYTHM_TRIGGERS = [
    # (节律标识, 触发类型, 置信度, 提示语)
    ("year_start", ReflectionTrigger.LONG_TERM_PLAN, 0.6, "年初，适合回顾和规划"),
    ("quarter_end", ReflectionTrigger.MAJOR_DECISION, 0.5, "季度末，适合复盘决策"),
    ("quarter_start", ReflectionTrigger.LONG_TERM_PLAN, 0.5, "季度初，适合调整方向"),
]


@dataclass
class TriggerEvent:
    """检测到的触发事件"""

    trigger: ReflectionTrigger
    confidence: float  # 触发置信度（0-1）
    source: str  # 触发来源：observation_mutation / rhythm / keyword / manual
    matched_signals: List[str]  # 匹配到的信号详情
    raw_text: str  # 原始输入文本或信号描述
    detected_at: datetime

    def to_dict(self):
        return {
            "trigger": self.trigger.value,
            "confidence": self.confidence,
            "source": self.source,
            "matched_signals": self.matched_signals,
            "raw_text": self.raw_text[:200],
            "detected_at": self.detected_at.isoformat(),
        }


@dataclass
class TriggerContext:
    """触发检测上下文"""

    recent_observations: List[Observation] = field(default_factory=list)
    last_trigger_ago_days: Optional[int] = None
    user_text: str = ""
    current_rhythm: str = ""


class TriggerDetector:
    """
    决策触发检测器

    使用方式：
        detector = TriggerDetector(observation_store=obs_store)

        # 方式1：基于上下文检测（推荐）
        event = detector.detect(context=TriggerContext(
            recent_observations=recent_obs,
            user_text="用户输入文本",
            current_rhythm="year_start",
        ))

        # 方式2：仅基于文本（兜底）
        event = detector.detect_text("我要启动新项目")
    """

    def __init__(
        self,
        observation_store: Optional[ObsStore] = None,
        sensitivity: float = 1.0,
        min_interval_hours: float = 4.0,  # 两次触发最小间隔
    ):
        self.obs_store = observation_store
        self.sensitivity = sensitivity
        self.min_interval = timedelta(hours=min_interval_hours)
        self._last_trigger_at: Optional[datetime] = None

    def detect(self, context: TriggerContext) -> Optional[TriggerEvent]:
        """
        综合检测触发信号

        检测顺序（优先级从高到低）：
        1. Observation 突变
        2. 时间节律
        3. 关键词匹配
        """
        now = datetime.now()

        # 检查最小间隔
        if self._last_trigger_at and (now - self._last_trigger_at) < self.min_interval:
            return None

        # 1. 检测 Observation 突变（主动）
        mutation_event = self._detect_observation_mutation(context)
        if mutation_event:
            self._last_trigger_at = now
            return mutation_event

        # 2. 检测时间节律（主动）
        rhythm_event = self._detect_rhythm_trigger(context)
        if rhythm_event:
            self._last_trigger_at = now
            return rhythm_event

        # 3. 关键词匹配（兜底）
        if context.user_text:
            keyword_event = self._detect_keywords(context.user_text)
            if keyword_event:
                self._last_trigger_at = now
                return keyword_event

        return None

    def detect_text(self, text: str) -> Optional[TriggerEvent]:
        """仅基于文本的检测（简化接口）"""
        return self.detect(TriggerContext(user_text=text))

    # ───────────────────────────────
    # Observation 突变检测（核心）
    # ───────────────────────────────

    def _detect_observation_mutation(self, context: TriggerContext) -> Optional[TriggerEvent]:
        """
        从 Observation 变化中检测行为信号

        策略：对比"最近窗口期"和"对比窗口期"的 Observation，
        如果某维度的特征发生显著变化，则视为触发信号。
        """
        if not self.obs_store and not context.recent_observations:
            return None

        now = datetime.now()
        best_event = None
        best_score = 0.0

        for dim, (recent_days, compare_days, min_score) in MUTATION_CONFIG.items():
            recent_window = now - timedelta(days=recent_days)
            compare_window = now - timedelta(days=compare_days)

            # 获取两个时间段的 Observations
            try:
                recent_obs = self._context_recent_observations(context, dim)
                if len(recent_obs) < 2:
                    if not self.obs_store:
                        continue
                    recent_obs = self._get_observations_in_range(
                        dimension=dim, start=recent_window, end=now
                    )
                if not self.obs_store:
                    continue
                compare_obs = self._get_observations_in_range(
                    dimension=dim, start=compare_window, end=recent_window
                )
            # DEBT(S8): 容错跳过，避免单条记录中断批量处理
            except (OSError, ValueError, TypeError, KeyError, ImportError, AttributeError, RuntimeError):
                continue

            if len(recent_obs) < 2 or len(compare_obs) < 2:
                continue  # 数据不足，跳过

            # 计算变化分数
            mutation_score = self._calculate_mutation_score(recent_obs, compare_obs, dim)
            effective_min = min_score / self.sensitivity

            if mutation_score >= effective_min and mutation_score > best_score:
                trigger_type = self._mutation_to_trigger_type(dim, mutation_score)
                best_score = mutation_score
                best_event = TriggerEvent(
                    trigger=trigger_type,
                    confidence=round(min(1.0, mutation_score), 2),
                    source="observation_mutation",
                    matched_signals=[
                        f"{dim} 维度突变: 近{recent_days}天 vs 前{compare_days}天",
                        f"变化分数: {mutation_score:.2f}",
                        f"近期样本: {len(recent_obs)}, 对比样本: {len(compare_obs)}",
                    ],
                    raw_text=f"检测到 {dim} 维度的行为模式突变",
                    detected_at=now,
                )

        return best_event

    def _context_recent_observations(
        self, context: TriggerContext, dimension: str
    ) -> List[Observation]:
        """Return caller-provided recent observations for one mutation dimension."""
        if not context.recent_observations:
            return []
        try:
            dim_enum = Dimension(dimension)
        except ValueError:
            return []
        return [obs for obs in context.recent_observations if obs.dimension == dim_enum]

    def _get_observations_in_range(
        self, dimension: str, start: datetime, end: datetime
    ) -> List[Observation]:
        """获取时间范围内的 Observations（P119: 将时间范围下推到 SQL，避免 limit 截断对比窗口）。"""
        try:
            dim_enum = Dimension(dimension)
            return self.obs_store.query(  # type: ignore[union-attr]
                dimension=dim_enum,
                period_start=start,
                period_end=end,
                limit=1000,
            )
        # DEBT(S8): 容错降级，返回默认值避免局部失败扩散
        except (
            OSError, ValueError, TypeError, KeyError, ImportError, AttributeError, RuntimeError
        ):
            return []

    def _calculate_mutation_score(
        self, recent: List[Observation], compare: List[Observation], dim: str
    ) -> float:
        """
        计算两个时间段 Observation 的变化分数

        不同维度使用不同检测策略：
        - growth: 角色标签变化
        - attention: 关注词分布变化
        - stress: 压力值均值变化
        - decisions: 决策模式变化
        - actions: 行动完成率变化
        """
        if dim == "attention":
            return self._mutation_attention(recent, compare)
        elif dim == "stress":
            return self._mutation_stress(recent, compare)
        elif dim == "growth":
            return self._mutation_growth(recent, compare)
        elif dim == "decisions":
            return self._mutation_decisions(recent, compare)
        elif dim == "actions":
            return self._mutation_actions(recent, compare)
        return 0.0

    def _mutation_attention(self, recent: List[Observation], compare: List[Observation]) -> float:
        """关注重心突变：词分布重叠度下降"""
        recent_words = self._extract_word_distribution(recent)
        compare_words = self._extract_word_distribution(compare)

        if not recent_words or not compare_words:
            return 0.0

        # 计算分布差异（1 - 重叠度）
        all_words = set(recent_words.keys()) | set(compare_words.keys())
        if not all_words:
            return 0.0

        # JS散度近似：比较两个分布
        diff_sum = 0.0
        for word in all_words:
            r = recent_words.get(word, 0)
            c = compare_words.get(word, 0)
            diff_sum += abs(r - c)

        return min(1.0, diff_sum / 2.0)

    def _mutation_stress(self, recent: List[Observation], compare: List[Observation]) -> float:
        """压力突变：近期压力均值显著高于对比期"""
        recent_vals = self._extract_numeric_values(recent)
        compare_vals = self._extract_numeric_values(compare)

        if not recent_vals or not compare_vals:
            return 0.0

        recent_mean = sum(recent_vals) / len(recent_vals)
        compare_mean = sum(compare_vals) / len(compare_vals)

        if compare_mean == 0:
            return 0.5 if recent_mean > 0 else 0.0

        # 压力上升的相对变化
        ratio = recent_mean / compare_mean
        if ratio > 2.0:
            return min(1.0, (ratio - 1.0) * 0.5)  # 翻倍以上视为显著
        return 0.0

    def _mutation_growth(self, recent: List[Observation], compare: List[Observation]) -> float:
        """成长突变：角色标签或关键词发生显著变化"""
        recent_text = " ".join(self._obs_value_text(o) for o in recent).lower()
        compare_text = " ".join(self._obs_value_text(o) for o in compare).lower()

        recent_words = set(recent_text.split())
        compare_words = set(compare_text.split())

        if not recent_words or not compare_words:
            return 0.0

        overlap = len(recent_words & compare_words) / max(len(recent_words), len(compare_words))
        return 1.0 - overlap

    def _mutation_decisions(self, recent: List[Observation], compare: List[Observation]) -> float:
        """决策模式突变：决策描述的关键词变化"""
        return self._mutation_growth(recent, compare)  # 复用文本重叠度

    def _mutation_actions(self, recent: List[Observation], compare: List[Observation]) -> float:
        """行动模式突变：完成率变化"""
        recent_completion = self._extract_completion_rate(recent)
        compare_completion = self._extract_completion_rate(compare)

        if recent_completion is None or compare_completion is None:
            return 0.0

        diff = abs(recent_completion - compare_completion)
        return min(1.0, diff * 2.0)  # 50%的变化对应满分

    # ───────────────────────────────
    # 时间节律检测
    # ───────────────────────────────

    def _detect_rhythm_trigger(self, context: TriggerContext) -> Optional[TriggerEvent]:
        """检测时间节律触发"""
        if not context.current_rhythm:
            return None

        for rhythm_id, trigger_type, base_conf, hint in RHYTHM_TRIGGERS:
            if context.current_rhythm == rhythm_id:
                return TriggerEvent(
                    trigger=trigger_type,
                    confidence=round(base_conf * self.sensitivity, 2),
                    source="rhythm",
                    matched_signals=[rhythm_id, hint],
                    raw_text=hint,
                    detected_at=datetime.now(),
                )

        return None

    # ───────────────────────────────
    # 关键词匹配（兜底）
    # ───────────────────────────────

    def _detect_keywords(self, text: str) -> Optional[TriggerEvent]:
        """基于关键词的触发检测"""
        if not text or len(text) < 5:
            return None

        text_lower = text.lower()
        best_trigger = None
        best_score = 0.0
        best_signals = []

        for trigger, patterns, threshold in TRIGGER_SIGNALS:
            score = 0.0
            matched = []

            for pattern, weight in patterns:
                if re.search(pattern, text_lower):
                    score += weight
                    matched.append(pattern)

            effective_threshold = threshold / self.sensitivity

            if score >= effective_threshold and score > best_score:
                best_trigger = trigger
                best_score = score
                best_signals = matched

        if not best_trigger:
            return None

        confidence = min(1.0, best_score / (best_score + 1.0))

        return TriggerEvent(
            trigger=best_trigger,
            confidence=round(confidence, 2),
            source="keyword",
            matched_signals=best_signals[:5],
            raw_text=text,
            detected_at=datetime.now(),
        )

    # ───────────────────────────────
    # 工具方法
    # ───────────────────────────────

    def _obs_value_text(self, obs: Observation) -> str:
        """提取 Observation 的文本表示"""
        if isinstance(obs.value, str):
            return obs.value
        elif isinstance(obs.value, dict):
            return " ".join(f"{k}:{v}" for k, v in obs.value.items())
        return str(obs.value)

    def _extract_word_distribution(self, observations: List[Observation]) -> Dict[str, float]:
        """从 Observations 中提取词频分布（归一化）"""
        from collections import Counter

        words = Counter()  # type: ignore[var-annotated]
        for obs in observations:
            text = self._obs_value_text(obs).lower()
            # 简单分词：中文2字以上，英文3字母以上
            for word in re.findall(r"[一-鿿]{2,}|[a-zA-Z_]{3,}", text):
                words[word] += 1

        total = sum(words.values())
        if total == 0:
            return {}
        return {w: c / total for w, c in words.items()}

    def _extract_numeric_values(self, observations: List[Observation]) -> List[float]:
        """提取数值型 value"""
        vals = []
        for obs in observations:
            if isinstance(obs.value, (int, float)):
                vals.append(float(obs.value))
            elif isinstance(obs.value, dict):
                for k in ["level", "score", "value", "mean", "avg"]:
                    if k in obs.value:
                        try:
                            vals.append(float(obs.value[k]))
                        except (ValueError, TypeError):
                            logging.getLogger(__name__).warning(
                                "[trigger_detector] (ValueError, TypeError) suppressed",
                                exc_info=True,
                            )
        return vals

    def _extract_completion_rate(self, observations: List[Observation]) -> Optional[float]:
        """提取完成率"""
        for obs in observations:
            if isinstance(obs.value, dict):
                for k in ["completion_rate", "完成率", "rate", "ratio"]:
                    if k in obs.value:
                        try:
                            return float(obs.value[k])
                        except (ValueError, TypeError):
                            logging.getLogger(__name__).warning(
                                "[trigger_detector] (ValueError, TypeError) suppressed",
                                exc_info=True,
                            )
        return None

    def _mutation_to_trigger_type(self, dim: str, score: float) -> ReflectionTrigger:
        """将维度突变映射到触发类型"""
        mapping = {
            "growth": ReflectionTrigger.ROLE_SHIFT,
            "attention": ReflectionTrigger.MAJOR_DECISION,
            "stress": ReflectionTrigger.MAJOR_DECISION,
            "decisions": ReflectionTrigger.MAJOR_DECISION,
            "actions": ReflectionTrigger.NEW_PROJECT,
        }
        return mapping.get(dim, ReflectionTrigger.MAJOR_DECISION)
