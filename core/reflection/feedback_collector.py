"""
Feedback Collector — 用户反馈收集器

核心职责：
1. 接收用户对 Insight 的反馈（👍 准 / 👎 不准 / 🤔 有启发 / 🚫 无关）
2. 关联反馈到具体的 ReflectionRecord
3. 标记待反馈的 Insight（用户还没反馈的）

使用方式：
    collector = FeedbackCollector(reflection_store)

    # 用户看完 Insight 后提交反馈
    collector.submit_feedback(
        reflection_id="abc123",
        feedback_type=FeedbackType.ACCURATE,
        comment="确实，我最近确实在纠结这个",
    )

    # 获取用户还没反馈的 Insight 列表
    pending = collector.get_pending_feedback(hours_since=24)

    # 获取反馈历史
    history = collector.get_feedback_history(limit=20)

设计原则：
- 反馈与 ReflectionRecord 强关联（知道用户对哪次 Insight 反馈）
- 支持可选评论（用户可以说为什么准/不准）
- 代码层实现，不依赖 Agent 推理
"""

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Dict, List, Optional

from core.reflection.models import (
    FeedbackType,
    ReflectionRecord,
)
from core.reflection.reflection_store import ReflectionStore

# Constants extracted from magic numbers
HOURS_AGO_SECONDS = 3600
FEEDBACK_COLLECTOR_GET_FEEDBACK_SUMMARY_DAYS = 30


@dataclass
class FeedbackResult:
    """反馈提交结果"""

    success: bool
    reflection_id: str
    feedback_type: str
    message: str = ""
    record: Optional[ReflectionRecord] = None


@dataclass
class PendingFeedbackItem:
    """待反馈的 Insight 项"""

    reflection_id: str
    created_at: datetime
    trigger: str
    insight_summary: str
    dimensions_involved: List[str]
    hours_ago: float


class FeedbackCollector:
    """用户反馈收集器"""

    def __init__(self, reflection_store: Optional[ReflectionStore] = None):
        self.ref_store = reflection_store or ReflectionStore()

    def submit_feedback(
        self,
        reflection_id: str,
        feedback_type: FeedbackType,
        comment: str = "",
    ) -> FeedbackResult:
        """Reject the retired direct writer; retained only for history reads."""

        del reflection_id, feedback_type, comment
        raise RuntimeError("legacy_reflection_feedback_write_retired")

    def get_pending_feedback(
        self,
        hours_since: float = 24.0,
        limit: int = 20,
    ) -> List[PendingFeedbackItem]:
        """
        获取待反馈的 Insight 列表

        策略：获取最近 hours_since 小时内生成、但还没有用户反馈的 Reflection。
        这可以用于在用户会话结束时主动询问反馈。

        Args:
            hours_since: 只查询最近多少小时内的 Reflection
            limit: 最多返回多少条

        Returns:
            List[PendingFeedbackItem]
        """
        cutoff = datetime.now() - timedelta(hours=hours_since)

        # 获取最近的 Reflection 记录
        records = self.ref_store.get_latest(limit=limit * 2)

        pending = []
        for record in records:
            # 过滤：没有反馈且在规定时间内
            if record.user_feedback is not None:
                continue
            if record.created_at < cutoff:
                continue
            if not record.insight:
                continue

            hours_ago = (datetime.now() - record.created_at).total_seconds() / HOURS_AGO_SECONDS
            pending.append(
                PendingFeedbackItem(
                    reflection_id=record.id,
                    created_at=record.created_at,
                    trigger=record.trigger.value,
                    insight_summary=record.insight.summary,
                    dimensions_involved=record.insight.dimensions_involved,
                    hours_ago=round(hours_ago, 1),
                )
            )

        return pending[:limit]

    def get_feedback_history(
        self,
        limit: int = 50,
        feedback_type: Optional[FeedbackType] = None,
    ) -> List[Dict]:
        """
        获取反馈历史

        Args:
            limit: 最多返回多少条
            feedback_type: 可选过滤特定类型

        Returns:
            List[Dict] 包含 feedback + 关联的 Reflection 信息
        """
        del limit, feedback_type
        return []

    def get_feedback_summary(
        self, days: int = FEEDBACK_COLLECTOR_GET_FEEDBACK_SUMMARY_DAYS
    ) -> Dict:
        """
        获取反馈汇总统计

        Args:
            days: 最近多少天的数据

        Returns:
            Dict with counts by type, response rate, etc.
        """
        return {
            "period_days": days,
            "total_reflections": 0,
            "with_feedback": 0,
            "response_rate": 0.0,
            "feedback_breakdown": {},
            "accuracy_rate": 0.0,
            "status": "legacy_feedback_quarantined_use_canonical_feedback_audit",
        }

    def _calc_accuracy_rate(self, counts: Dict[str, int]) -> float:
        """计算准确率（accurate + insightful / 有反馈的总数）"""
        positive = counts.get("accurate", 0) + counts.get("insightful", 0)
        total_with_feedback = sum(counts.values())
        if total_with_feedback == 0:
            return 0.0
        return round(positive / total_with_feedback, 2)
