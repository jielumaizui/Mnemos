"""
Implicit Feedback Detector — 隐式反馈检测器

核心职责：
从用户的自然会话行为中推断对 Insight 的反馈，无需用户手动点击 👍/👎。

设计约束（用户明确拒绝关键词匹配）：
- 不分析消息内容的语义（不检测"对"/"不对"/"说到点了"）
- 不依赖 LLM 做意图识别
- 只依赖会话结构指标：消息数量、长度、时间间隔、会话终止模式

信号逻辑：
┌─────────────────────────────────────────────────────────────┐
│ 结构指标              │ 推断反馈                            │
├─────────────────────────────────────────────────────────────┤
│ Insight 后 0 条消息   │ IRRELEVANT（用户直接离开）           │
│ Insight 后 1-2 条短消息│ IRRELEVANT（敷衍回应）              │
│ 首条响应间隔 > 5 分钟 │ IRRELEVANT（用户可能忽略）           │
│ Insight 后 ≥3 条消息  │ ACCURATE（用户继续深入讨论）         │
│ 会话总时长因 Insight 延长 2x │ INSIGHTFUL（激发深入思考）     │
└─────────────────────────────────────────────────────────────┘

权重策略：
- 隐式反馈的置信度低于显式反馈（0.5 vs 1.0）
- 多个隐式信号冲突时，取最高置信度信号
- 隐式反馈不覆盖显式反馈，而是补充
"""

from dataclasses import dataclass, field
from datetime import datetime
from typing import List, Optional

from core.reflection.models import FeedbackType, UserFeedback


@dataclass
class SessionContext:
    """会话上下文 — 用于隐式反馈推断"""

    reflection_id: str
    insight_generated_at: datetime
    session_started_at: datetime
    session_ended_at: Optional[datetime] = None

    # Insight 生成后的用户行为
    messages_after_insight: int = 0  # Insight 后用户发了多少条消息
    avg_message_length_after: int = 0  # Insight 后消息平均长度（字符）
    time_to_first_response_sec: Optional[float] = None  # 首条响应间隔（秒）
    session_extended: bool = False  # Insight 是否延长了会话

    # 可选：宿主 Agent 提供的结构化信号
    explicit_signals: List[str] = field(default_factory=list)
    # explicit_signals 可能包含 "user_acted_on_insight", "user_ignored", "session_abrupt_end"
    # 这些由宿主 Agent 在代码层标记，不是语义推断


@dataclass
class ImplicitFeedback:
    """隐式反馈结果"""

    reflection_id: str
    inferred_type: FeedbackType
    confidence: float  # 推断置信度（0.0-1.0，低于显式反馈）
    signals: List[str]  # 触发推断的结构信号列表
    inferred_at: datetime = field(default_factory=datetime.now)

    def to_user_feedback(self) -> UserFeedback:
        """转换为 UserFeedback 格式（供校准器消费）"""
        comment = f"[隐式反馈] 基于信号: {', '.join(self.signals)}"
        return UserFeedback(
            feedback_type=self.inferred_type,
            comment=comment,
            given_at=self.inferred_at,
        )


class ImplicitFeedbackDetector:
    """隐式反馈检测器 — 基于会话结构，零语义分析"""

    # 阈值配置
    SHORT_MESSAGE_THRESHOLD = 15  # 短消息字符数阈值
    LONG_RESPONSE_DELAY = 300  # 长响应延迟（5分钟，秒）
    DEEP_ENGAGEMENT_MESSAGES = 3  # 深入讨论的消息数阈值
    SESSION_EXTENSION_RATIO = 2.0  # Insight 后会话时长至少达到前段的倍数
    MIN_CONFIDENCE = 0.3  # 最低推断置信度

    def _session_extension_signal(self, context: SessionContext) -> Optional[str]:
        """Derive session extension from timestamps when the host did not flag it."""
        if context.session_ended_at is None:
            return None
        if context.session_started_at >= context.insight_generated_at:
            return None
        if context.session_ended_at <= context.insight_generated_at:
            return None

        before_insight_sec = (
            context.insight_generated_at - context.session_started_at
        ).total_seconds()
        after_insight_sec = (
            context.session_ended_at - context.insight_generated_at
        ).total_seconds()
        if before_insight_sec <= 0 or after_insight_sec <= 0:
            return None
        if after_insight_sec < before_insight_sec * self.SESSION_EXTENSION_RATIO:
            return None

        return (
            "session_extended_by_duration("
            f"{after_insight_sec:.0f}s_after/{before_insight_sec:.0f}s_before)"
        )

    def detect(self, context: SessionContext) -> Optional[ImplicitFeedback]:
        """
        从会话结构中推断反馈

        Args:
            context: 会话上下文

        Returns:
            ImplicitFeedback 或 None（信号不足）
        """
        signals = []
        scores = {}  # feedback_type -> (score, signals)

        # ─── 信号 1: 零响应 ───
        if context.messages_after_insight == 0:
            signals.append("zero_response_after_insight")
            scores[FeedbackType.IRRELEVANT] = (0.7, signals.copy())

        # ─── 信号 2: 短消息敷衍 ───
        if (
            0 < context.messages_after_insight <= 2
            and context.avg_message_length_after < self.SHORT_MESSAGE_THRESHOLD
        ):
            signals.append(f"short_response({context.avg_message_length_after}chars)")
            scores[FeedbackType.IRRELEVANT] = (
                max(scores.get(FeedbackType.IRRELEVANT, (0, []))[0], 0.6),
                signals.copy(),
            )

        # ─── 信号 3: 长延迟响应 ───
        if (
            context.time_to_first_response_sec is not None
            and context.time_to_first_response_sec > self.LONG_RESPONSE_DELAY
        ):
            signals.append(f"delayed_response({context.time_to_first_response_sec:.0f}s)")
            scores[FeedbackType.IRRELEVANT] = (
                max(scores.get(FeedbackType.IRRELEVANT, (0, []))[0], 0.55),
                signals.copy(),
            )

        # ─── 信号 4: 深入讨论 ───
        if context.messages_after_insight >= self.DEEP_ENGAGEMENT_MESSAGES:
            signals.append(f"deep_engagement({context.messages_after_insight}msgs)")
            scores[FeedbackType.ACCURATE] = (0.65, signals.copy())

        # ─── 信号 5: 会话延长 ───
        session_extension_signals = []
        if context.session_extended:
            session_extension_signals.append("session_extended")
        duration_signal = self._session_extension_signal(context)
        if duration_signal:
            session_extension_signals.append(duration_signal)

        if session_extension_signals:
            signals.extend(session_extension_signals)
            scores[FeedbackType.INSIGHTFUL] = (
                max(scores.get(FeedbackType.INSIGHTFUL, (0, []))[0], 0.6),
                signals.copy(),
            )

        # ─── 信号 6: 宿主 Agent 显式标记 ───
        for sig in context.explicit_signals:
            if sig == "user_acted_on_insight":
                signals.append("host_marked:action_taken")
                scores[FeedbackType.ACCURATE] = (
                    max(scores.get(FeedbackType.ACCURATE, (0, []))[0], 0.8),
                    signals.copy(),
                )
            elif sig == "user_ignored":
                signals.append("host_marked:ignored")
                scores[FeedbackType.IRRELEVANT] = (
                    max(scores.get(FeedbackType.IRRELEVANT, (0, []))[0], 0.75),
                    signals.copy(),
                )
            elif sig == "session_abrupt_end":
                signals.append("host_marked:abrupt_end")
                scores[FeedbackType.IRRELEVANT] = (
                    max(scores.get(FeedbackType.IRRELEVANT, (0, []))[0], 0.7),
                    signals.copy(),
                )

        # ─── 冲突解决 ───
        # 如果同时有正负信号，优先取置信度高的
        # 如果置信度相同，IRRELEVANT 优先（保守策略：不确定就当没中）
        if not scores:
            return None

        best_type, (best_score, best_signals) = max(
            scores.items(), key=lambda x: (x[1][0], -1 if x[0] == FeedbackType.IRRELEVANT else 0)
        )

        if best_score < self.MIN_CONFIDENCE:
            return None

        return ImplicitFeedback(
            reflection_id=context.reflection_id,
            inferred_type=best_type,
            confidence=round(best_score, 2),
            signals=best_signals,
        )

    def detect_simple(
        self,
        reflection_id: str,
        messages_after: int,
        avg_length_after: int,
        session_ended_immediately: bool = False,
    ) -> Optional[ImplicitFeedback]:
        """
        简化版检测 — 只需要最基本的结构指标

        适用于宿主 Agent 无法提供完整会话上下文的情况
        """
        context = SessionContext(
            reflection_id=reflection_id,
            insight_generated_at=datetime.now(),
            session_started_at=datetime.now(),
            messages_after_insight=messages_after,
            avg_message_length_after=avg_length_after,
            explicit_signals=["session_abrupt_end"] if session_ended_immediately else [],
        )
        return self.detect(context)

    @staticmethod
    def should_collect_feedback(reflection_age_hours: float, has_explicit_feedback: bool) -> bool:
        """
        判断是否应该向用户收集显式反馈

        策略：
        - 如果已有显式反馈，不再收集
        - 如果 Insight 生成超过 24 小时且没有隐式信号，主动询问
        - 如果已有强隐式信号，不打扰用户
        """
        if has_explicit_feedback:
            return False
        if reflection_age_hours > 24:
            return True
        return False
