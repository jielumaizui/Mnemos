"""
Feedback Loop — 认知变迁反哺机制

核心职责：
1. 从 Reflection 记录中检测认知变迁（CognitiveShift）
2. 将认知变迁反哺为新的 Observation（Layer 3）
3. 将认知变迁建议更新到 Knowledge（Layer 2）

反哺路径：
    ReflectionRecord → CognitiveShift → 新 Observation → ObservationStore
    ReflectionRecord → CognitiveShift → 知识更新建议 → Wiki

设计原则：
- 代码层实现变迁检测（不依赖 LLM 推理）
- 变迁必须有证据支撑（confidence 阈值）
- 避免过度敏感（最小时间间隔、最小变化幅度）
"""

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Dict, Iterator, List, Optional

from core.access_policy import AccessNarrowing, PrincipalEnvelope

from core.cognitive.models import Dimension, Observation, ObservationType
from core.cognitive.observation_store import ObservationStore as ObsStore
from core.reflection.models import CognitiveShift, ReflectionRecord
from core.reflection.reflection_store import ReflectionStore

logger = logging.getLogger(__name__)

# 变迁检测配置
SHIFT_CONFIG = {
    # 维度 → (最小置信度变化, 最小时间间隔天数)
    "growth": (0.3, 30),  # 角色变化需要较大变化，至少间隔30天
    "attention": (0.4, 14),  # 关注转移需要较大变化
    "decisions": (0.3, 21),  # 决策模式变化
    "stress": (0.5, 7),  # 压力变化快，但要求变化幅度大
    "actions": (0.3, 14),
    "time": (0.4, 60),  # 时间估算偏差变化慢
    "relationships": (0.3, 30),
}

KNOWLEDGE_UPDATE_SUGGESTIONS = {
    "role_change_to_manager": "用户角色可能已转变为管理者，建议更新相关知识和项目记录",
    "role_change_to_expert": "用户可能已成为领域专家，建议整理专业知识库",
    "role_change_to_founder": "用户可能有创业动向，建议更新商业相关笔记",
    "focus_shift": "用户关注重心已转移，建议更新 MOC 和项目优先级",
    "style_become_decisive": "用户决策风格趋于果断，建议更新决策方法论",
    "stress_decrease": "用户压力水平下降，建议记录有效的压力管理方法",
    "execution_improvement": "用户执行力提升，建议复盘有效的工作方法",
}


def knowledge_update_from_shift(shift: CognitiveShift) -> Dict:
    """Derive the replayable knowledge suggestion from one canonical shift."""

    suggestion = KNOWLEDGE_UPDATE_SUGGESTIONS.get(
        shift.shift_type,
        "用户认知发生变化，建议检查相关 Wiki 页面",
    )
    return {
        "dimension": shift.dimension,
        "shift_type": shift.shift_type,
        "suggestion": suggestion,
        "confidence": shift.confidence,
        "from_state": shift.from_state,
        "to_state": shift.to_state,
        "detected_at": shift.shift_detected_at.isoformat(),
    }


@dataclass
class FeedbackResult:
    """反哺结果"""

    shifts_detected: List[CognitiveShift] = None  # type: ignore[assignment]
    new_observations: List[Observation] = None  # type: ignore[assignment]
    knowledge_updates: List[Dict] = None  # type: ignore[assignment]
    messages: List[str] = None  # type: ignore[assignment]

    def __post_init__(self):
        if self.shifts_detected is None:
            self.shifts_detected = []
        if self.new_observations is None:
            self.new_observations = []
        if self.knowledge_updates is None:
            self.knowledge_updates = []
        if self.messages is None:
            self.messages = []


class FeedbackLoop:
    """认知变迁反哺引擎"""

    def __init__(
        self,
        reflection_store: Optional[ReflectionStore] = None,
        observation_store: Optional[ObsStore] = None,
    ):
        self.ref_store = reflection_store
        self.obs_store = observation_store

    def process_reflection(
        self,
        record: ReflectionRecord,
        *,
        principal: PrincipalEnvelope | None = None,
        narrowing: AccessNarrowing | None = None,
    ) -> FeedbackResult:
        """
        处理一次 Reflection，检测认知变迁并反哺

        Args:
            record: 刚生成的 Reflection 记录

        Returns:
            FeedbackResult
        """
        result = FeedbackResult()

        if not self.ref_store:
            result.messages.append("ReflectionStore 未配置，跳过反哺")
            return result

        # 1. 检测认知变迁
        shifts = list(
            self._detect_shifts(
                record,
                principal=principal,
                narrowing=narrowing,
            )
        )
        result.shifts_detected = shifts

        for shift in shifts:
            # 2. 保存认知变迁
            self.ref_store.save_shift(shift, record.id)
            result.messages.append(
                f"检测到认知变迁 [{shift.dimension}]: {shift.from_state} → {shift.to_state} "
                f"(置信度: {shift.confidence:.2f})"
            )

            # 3. 反哺为新的 Observation
            if self.obs_store:
                new_obs = self._shift_to_observation(shift)
                if new_obs:
                    self.obs_store.save(new_obs)
                    result.new_observations.append(new_obs)
                    result.messages.append(f"  → 已生成新 Observation: {new_obs.dimension.value}")

            # 4. 生成知识更新建议
            knowledge_suggestion = self._shift_to_knowledge_update(shift)
            if knowledge_suggestion:
                result.knowledge_updates.append(knowledge_suggestion)
                result.messages.append(
                    f"  → 知识更新建议: {knowledge_suggestion['suggestion'][:60]}"
                )

        # 5. 标记反哺完成
        if shifts:
            self.ref_store.mark_fed_back(
                record.id,
                to_observations=bool(result.new_observations),
                to_knowledge=bool(result.knowledge_updates),
            )

        return result

    def _detect_shifts(
        self,
        record: ReflectionRecord,
        *,
        principal: PrincipalEnvelope | None,
        narrowing: AccessNarrowing | None,
    ) -> Iterator[CognitiveShift]:
        """
        从 Reflection 记录中检测认知变迁

        策略：对比本次 Reflection 和历史上同一维度的 Reflection，
        如果发现显著变化，则标记为认知变迁。
        """
        if not record.mirror_snapshots:
            return

        # 获取历史 Reflection（同一触发类型或同维度）
        history: List[ReflectionRecord] = []
        if self.ref_store:
            try:
                history, _summary = self.ref_store.authorized_get_by_trigger(
                    record.trigger,
                    principal=principal,
                    narrowing=narrowing,
                    purpose="reflection_prompt",
                    limit=10,
                )
            except (OSError, ValueError, TypeError, KeyError, ImportError, AttributeError, RuntimeError):
                logger.warning("读取 Reflection 历史失败", exc_info=True)

        # 按维度对比
        for snapshot in record.mirror_snapshots:
            dim = snapshot.dimension
            dim_config = SHIFT_CONFIG.get(dim)
            if not dim_config:
                continue

            min_delta, min_interval = dim_config

            # 查找历史上同一维度的记录
            dim_history = [
                h
                for h in history
                if h.id != record.id and any(s.dimension == dim for s in h.mirror_snapshots)
            ]

            if not dim_history:
                # 首次记录该维度，不视为变迁（没有对比基准）
                continue

            # 取最近的一次历史记录
            latest_history = max(dim_history, key=lambda h: h.created_at)

            # 检查时间间隔
            days_since = (record.created_at - latest_history.created_at).days
            if days_since < min_interval:
                continue  # 变化太快，可能是噪声

            # 获取历史 snapshot
            hist_snapshot = None
            for s in latest_history.mirror_snapshots:
                if s.dimension == dim:
                    hist_snapshot = s
                    break

            if not hist_snapshot:
                continue

            # 检测变化
            shift = self._compare_snapshots(
                dim=dim,
                current=snapshot,
                previous=hist_snapshot,
                days_since=days_since,
                min_delta=min_delta,
            )
            if shift:
                yield shift

    def _compare_snapshots(
        self,
        dim: str,
        current,
        previous,
        days_since: int,
        min_delta: float,
    ) -> Optional[CognitiveShift]:
        """
        对比两个时间点的 snapshot，检测是否有认知变迁

        Returns:
            CognitiveShift if change detected, else None
        """
        # 简单策略：如果 value_summary 发生显著变化，则视为变迁
        # 更复杂的策略可以解析 value 结构进行对比

        current_val = current.value_summary.lower()
        previous_val = previous.value_summary.lower()

        # 完全没变
        if current_val == previous_val:
            return None

        # 计算变化幅度（简单版本：关键词重叠度）
        current_words = set(current_val.split())
        previous_words = set(previous_val.split())

        if not current_words or not previous_words:
            return None

        overlap = len(current_words & previous_words) / max(len(current_words), len(previous_words))
        change_score = 1.0 - overlap

        if change_score < min_delta:
            return None

        # 确定变迁类型
        shift_type = self._infer_shift_type(dim, current_val, previous_val)

        return CognitiveShift(
            dimension=dim,
            shift_type=shift_type,
            from_state=previous.value_summary[:50],
            to_state=current.value_summary[:50],
            confidence=round(change_score, 2),
            evidence=[
                f"{days_since}天前: {previous.value_summary[:80]}",
                f"现在: {current.value_summary[:80]}",
            ],
            first_seen_at=previous.period_end or datetime.now() - timedelta(days=days_since),
        )

    def _infer_shift_type(self, dim: str, current: str, previous: str) -> str:
        """推断变迁类型"""
        # Growth 维度
        if dim == "growth":
            if "管理" in current and "管理" not in previous:
                return "role_change_to_manager"
            if "专家" in current and "专家" not in previous:
                return "role_change_to_expert"
            if "创始人" in current and "创始人" not in previous:
                return "role_change_to_founder"
            return "identity_evolution"

        # Attention 维度
        if dim == "attention":
            return "focus_shift"

        # Decisions 维度
        if dim == "decisions":
            if "犹豫" in previous and "犹豫" not in current:
                return "style_become_decisive"
            return "decision_pattern_change"

        # Stress 维度
        if dim == "stress":
            if current.count("压力") < previous.count("压力"):
                return "stress_decrease"
            return "stress_change"

        # Time 维度
        if dim == "time":
            return "time_estimation_change"

        # Actions 维度
        if dim == "actions":
            if "完成" in current and "完成" not in previous:
                return "execution_improvement"
            return "action_pattern_change"

        return "general_shift"

    def _shift_to_observation(self, shift: CognitiveShift) -> Optional[Observation]:
        """
        将认知变迁转换为新的 Observation

        这样 Layer 4 的产出可以反哺 Layer 3，形成闭环
        """
        try:
            dim = Dimension(shift.dimension)
        except ValueError:
            return None

        return Observation(
            dimension=dim,
            observation_type=ObservationType.TREND,  # 变迁是趋势型
            value={
                "shift_type": shift.shift_type,
                "from_state": shift.from_state,
                "to_state": shift.to_state,
                "confidence": shift.confidence,
            },
            unit="shift",
            confidence=shift.confidence,
            source_path=f"reflection_feedback:{shift.shift_type}",
            source_id="feedback_loop",
            evidence=[
                f"认知变迁: {shift.from_state} → {shift.to_state}",
                f"变迁类型: {shift.shift_type}",
                f"检测时间: {shift.shift_detected_at.strftime('%Y-%m-%d')}",
            ],
            observed_at=shift.shift_detected_at,
            period_start=shift.first_seen_at,
            period_end=shift.shift_detected_at,
        )

    def _shift_to_knowledge_update(self, shift: CognitiveShift) -> Optional[Dict]:
        """
        生成知识更新建议

        这些建议可以指导 Layer 2 的 Wiki 更新
        """
        return knowledge_update_from_shift(shift)
