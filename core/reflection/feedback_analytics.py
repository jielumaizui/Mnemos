import logging

"""
Feedback Analytics — 反馈数据分析器

核心职责：
1. 统计各维度/触发类型的 Insight 有效性
2. 识别高价值和低价值的 Insight 模式
3. 生成反馈趋势报告
4. 为 InsightCalibrator 提供数据支撑

使用方式：
    analytics = FeedbackAnalytics(reflection_store)

    # 各维度有效性
    dim_stats = analytics.effectiveness_by_dimension(days=30)
    # {"attention": {"total": 10, "positive": 7, "rate": 0.7}, ...}

    # 各触发类型有效性
    trigger_stats = analytics.effectiveness_by_trigger(days=30)

    # 趋势报告
    report = analytics.get_insight_quality_report(days=30)

设计原则：
- 所有计算在代码层完成（不依赖 Agent 推理）
- 正反馈 = accurate + insightful，负反馈 = inaccurate + irrelevant
- 支持时间窗口过滤
- 输出可直接被 calibrator 消费
"""

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

from core.reflection.models import FeedbackType
from core.reflection.reflection_store import ReflectionStore

# Constants extracted from magic numbers

logger = logging.getLogger(__name__)
FEEDBACK_ANALYTICS_EFFECTIVENESS_BY_DIMENSION_MIN_SAMPLES_DAYS = 30
FEEDBACK_ANALYTICS_EFFECTIVENESS_BY_TRIGGER_MIN_SAMPLES_DAYS = 30
FEEDBACK_ANALYTICS_TREND_OVER_TIME_WINDOW_DAYS_DAYS = 90
WINDOW_DAYS = 7
FEEDBACK_ANALYTICS_GET_INSIGHT_QUALITY_REPORT_DAYS = 30
TREND_DAYS = 90
TREND_DAYS_2 = 7


@dataclass
class DimensionEffectiveness:
    """某个维度的有效性统计"""

    dimension: str
    total: int = 0
    positive: int = 0  # accurate + insightful
    negative: int = 0  # inaccurate + irrelevant
    accuracy_rate: float = 0.0  # positive / total_with_feedback
    response_rate: float = 0.0  # with_feedback / total
    trend: str = "stable"  # improving / declining / stable / insufficient_data


@dataclass
class TriggerEffectiveness:
    """某个触发类型的有效性统计"""

    trigger: str
    total: int = 0
    positive: int = 0
    negative: int = 0
    accuracy_rate: float = 0.0
    avg_confidence: float = 0.0  # Insight 的平均置信度


@dataclass
class FeedbackTrend:
    """反馈趋势（按时间窗口）"""

    window_label: str  # "2024-W01", "Jan" 等
    total: int = 0
    positive: int = 0
    negative: int = 0
    rate: float = 0.0


class FeedbackAnalytics:
    """反馈数据分析器"""

    def __init__(self, reflection_store: Optional[ReflectionStore] = None):
        self.ref_store = reflection_store or ReflectionStore()

    # ───────────────────────────────
    # 核心分析接口
    # ───────────────────────────────

    def effectiveness_by_dimension(
        self,
        days: int = FEEDBACK_ANALYTICS_EFFECTIVENESS_BY_DIMENSION_MIN_SAMPLES_DAYS,
        min_samples: int = 3,
    ) -> Dict[str, DimensionEffectiveness]:
        """
        各维度的 Insight 有效性统计

        Args:
            days: 时间窗口
            min_samples: 最少样本数（低于此数量的维度标记为数据不足）

        Returns:
            Dict[dimension, DimensionEffectiveness]
        """
        records = self._get_records_with_any_feedback(days)

        # 按维度分组统计
        dim_stats: Dict[str, Dict] = {}
        for record in records:
            effective_fb = self._get_effective_feedback(record)
            if not effective_fb:
                continue
            for dim in record.mirror_dimensions:
                if dim not in dim_stats:
                    dim_stats[dim] = {"total": 0, "positive": 0, "negative": 0, "with_feedback": 0}

                dim_stats[dim]["total"] += 1
                dim_stats[dim]["with_feedback"] += 1
                if self._is_positive(effective_fb):
                    dim_stats[dim]["positive"] += 1
                else:
                    dim_stats[dim]["negative"] += 1

        # 计算有效性
        result = {}
        for dim, stats in sorted(dim_stats.items()):
            total = stats["total"]
            with_fb = stats["with_feedback"]
            positive = stats["positive"]
            negative = stats["negative"]

            if with_fb < min_samples:
                trend = "insufficient_data"
                acc_rate = 0.0
            elif with_fb > 0:
                acc_rate = round(positive / with_fb, 2)
                trend = self._calc_trend(dim, days)
            else:
                acc_rate = 0.0
                trend = "insufficient_data"

            result[dim] = DimensionEffectiveness(
                dimension=dim,
                total=total,
                positive=positive,
                negative=negative,
                accuracy_rate=acc_rate,
                response_rate=round(with_fb / total, 2) if total > 0 else 0.0,
                trend=trend,
            )

        return result

    def effectiveness_by_trigger(
        self,
        days: int = FEEDBACK_ANALYTICS_EFFECTIVENESS_BY_TRIGGER_MIN_SAMPLES_DAYS,
        min_samples: int = 2,
    ) -> Dict[str, TriggerEffectiveness]:
        """
        各触发类型的 Insight 有效性统计

        Args:
            days: 时间窗口
            min_samples: 最少样本数

        Returns:
            Dict[trigger, TriggerEffectiveness]
        """
        records = self._get_records_with_any_feedback(days)

        trigger_stats: Dict[str, Dict] = {}
        for record in records:
            effective_fb = self._get_effective_feedback(record)
            if not effective_fb:
                continue

            trigger = record.trigger.value
            if trigger not in trigger_stats:
                trigger_stats[trigger] = {
                    "total": 0,
                    "positive": 0,
                    "negative": 0,
                    "with_feedback": 0,
                    "confidence_sum": 0.0,
                    "confidence_count": 0,
                }

            trigger_stats[trigger]["total"] += 1
            trigger_stats[trigger]["with_feedback"] += 1
            if self._is_positive(effective_fb):
                trigger_stats[trigger]["positive"] += 1
            else:
                trigger_stats[trigger]["negative"] += 1

            # 累加 insight 置信度（如果有的话）
            if record.insight:
                conf = self._extract_insight_confidence(record)
                if conf > 0:
                    trigger_stats[trigger]["confidence_sum"] += conf
                    trigger_stats[trigger]["confidence_count"] += 1

        result = {}
        for trigger, stats in sorted(trigger_stats.items()):
            total = stats["total"]
            with_fb = stats["with_feedback"]
            positive = stats["positive"]
            negative = stats["negative"]

            if with_fb >= min_samples and with_fb > 0:
                acc_rate = round(positive / with_fb, 2)
            else:
                acc_rate = 0.0

            avg_conf = 0.0
            if stats["confidence_count"] > 0:
                avg_conf = round(stats["confidence_sum"] / stats["confidence_count"], 2)

            result[trigger] = TriggerEffectiveness(
                trigger=trigger,
                total=total,
                positive=positive,
                negative=negative,
                accuracy_rate=acc_rate,
                avg_confidence=avg_conf,
            )

        return result

    def trend_over_time(
        self,
        days: int = FEEDBACK_ANALYTICS_TREND_OVER_TIME_WINDOW_DAYS_DAYS,
        window_days: int = WINDOW_DAYS,
    ) -> List[FeedbackTrend]:
        """
        反馈趋势（按时间窗口）

        Args:
            days: 总时间范围
            window_days: 每个窗口的天数

        Returns:
            List[FeedbackTrend] 按时间顺序
        """
        records = self._get_records_with_any_feedback(days)

        # 按窗口分组
        now = datetime.now()
        windows: Dict[str, Dict] = {}

        for record in records:
            effective_fb = self._get_effective_feedback(record)
            if not effective_fb:
                continue

            # 计算属于哪个窗口
            days_ago = (now - record.created_at).days
            window_idx = days_ago // window_days
            window_start = now - timedelta(days=(window_idx + 1) * window_days)
            window_end = now - timedelta(days=window_idx * window_days)
            label = f"{window_start.strftime('%m/%d')}-{window_end.strftime('%m/%d')}"

            if label not in windows:
                windows[label] = {"total": 0, "positive": 0, "negative": 0}

            windows[label]["total"] += 1
            if self._is_positive(effective_fb):
                windows[label]["positive"] += 1
            else:
                windows[label]["negative"] += 1

        # 按时间顺序排序（label 是日期范围，需要按时间倒序然后反转）
        sorted_labels = sorted(windows.keys())
        result = []
        for label in sorted_labels:
            stats = windows[label]
            total = stats["total"]
            positive = stats["positive"]
            rate = round(positive / total, 2) if total > 0 else 0.0
            result.append(
                FeedbackTrend(
                    window_label=label,
                    total=total,
                    positive=positive,
                    negative=stats["negative"],
                    rate=rate,
                )
            )

        return result

    def get_insight_quality_report(
        self, days: int = FEEDBACK_ANALYTICS_GET_INSIGHT_QUALITY_REPORT_DAYS
    ) -> Dict:
        """
        生成 Insight 质量综合报告

        Returns:
            Dict 包含整体统计、各维度/触发类型排名、改进建议
        """
        dim_eff = self.effectiveness_by_dimension(days)
        trigger_eff = self.effectiveness_by_trigger(days)
        trend = self.trend_over_time(min(days, TREND_DAYS), window_days=TREND_DAYS_2)

        overall = self._aggregate_overall(dim_eff)
        dim_ranked = self._rank_dimensions(dim_eff, min_samples=3)
        trigger_ranked = self._rank_triggers(trigger_eff, min_samples=2)
        problem_dims = self._identify_problem_dims(dim_eff)
        high_value_dims = self._identify_high_value_dims(dim_eff)
        trend_direction = self._trend_direction(trend)

        return {
            "period_days": days,
            "overall": overall,
            "dimensions": {
                "ranked": [
                    {
                        "dimension": d.dimension,
                        "accuracy_rate": d.accuracy_rate,
                        "total": d.total,
                        "trend": d.trend,
                    }
                    for d in dim_ranked
                ],
                "problematic": problem_dims,
                "high_value": high_value_dims,
            },
            "triggers": {
                "ranked": [
                    {
                        "trigger": t.trigger,
                        "accuracy_rate": t.accuracy_rate,
                        "total": t.total,
                    }
                    for t in trigger_ranked
                ],
            },
            "trend": {
                "direction": trend_direction,
                "weekly_windows": [
                    {"label": t.window_label, "rate": t.rate, "total": t.total} for t in trend
                ],
            },
        }

    @staticmethod
    def _aggregate_overall(dim_eff: Dict[str, DimensionEffectiveness]) -> Dict[str, Any]:
        """汇总整体反馈统计。"""
        total_positive = sum(d.positive for d in dim_eff.values())
        total_negative = sum(d.negative for d in dim_eff.values())
        total_with_feedback = total_positive + total_negative

        overall_rate = (
            round(total_positive / total_with_feedback, 2) if total_with_feedback > 0 else 0.0
        )

        return {
            "total_with_feedback": total_with_feedback,
            "positive": total_positive,
            "negative": total_negative,
            "accuracy_rate": overall_rate,
        }

    @staticmethod
    def _rank_dimensions(
        dim_eff: Dict[str, DimensionEffectiveness], min_samples: int
    ) -> List[DimensionEffectiveness]:
        """按准确率对维度排名。"""
        return sorted(
            [d for d in dim_eff.values() if d.total >= min_samples],
            key=lambda x: x.accuracy_rate,
            reverse=True,
        )

    @staticmethod
    def _rank_triggers(
        trigger_eff: Dict[str, TriggerEffectiveness], min_samples: int
    ) -> List[TriggerEffectiveness]:
        """按准确率对触发类型排名。"""
        return sorted(
            [t for t in trigger_eff.values() if t.total >= min_samples],
            key=lambda x: x.accuracy_rate,
            reverse=True,
        )

    @staticmethod
    def _identify_problem_dims(dim_eff: Dict[str, DimensionEffectiveness]) -> List[str]:
        """识别准确率低于 0.5 的问题维度。"""
        return [
            d.dimension for d in dim_eff.values() if d.accuracy_rate < 0.5 and d.total >= 3
        ]

    @staticmethod
    def _identify_high_value_dims(dim_eff: Dict[str, DimensionEffectiveness]) -> List[str]:
        """识别准确率不低于 0.8 的高价值维度。"""
        return [
            d.dimension for d in dim_eff.values() if d.accuracy_rate >= 0.8 and d.total >= 3
        ]

    @staticmethod
    def _trend_direction(trend: List[FeedbackTrend], threshold: float = 0.1) -> str:
        """根据时间窗口趋势判断方向。"""
        if len(trend) < 2:
            return "stable"

        mid = len(trend) // 2
        first_half = sum(t.rate for t in trend[:mid]) / max(1, mid)
        second_half = sum(t.rate for t in trend[mid:]) / max(1, len(trend) - mid)

        if second_half > first_half + threshold:
            return "improving"
        if second_half < first_half - threshold:
            return "declining"
        return "stable"

    # ───────────────────────────────
    # 辅助方法
    # ───────────────────────────────

    def _get_records_with_any_feedback(self, days: int):
        """Historical reflection columns are quarantined, never active evidence."""

        del days
        return []

    def _get_effective_feedback(self, record) -> Optional[FeedbackType]:
        del record
        return None

    def _has_any_feedback(self, record) -> bool:
        """判断记录是否有任意反馈源"""
        return self._get_effective_feedback(record) is not None

    def _is_positive(self, feedback_type: FeedbackType) -> bool:
        """判断是否为正反馈"""
        return feedback_type in (FeedbackType.ACCURATE, FeedbackType.INSIGHTFUL)

    def _extract_insight_confidence(self, record) -> float:
        """从 record 中提取 insight 置信度"""
        # 优先从 temporal_context 中查找
        if record.temporal_context and "insight_confidence" in record.temporal_context:
            try:
                return float(record.temporal_context["insight_confidence"])
            except (ValueError, TypeError):
                logging.getLogger(__name__).warning(
                    "[feedback_analytics] (ValueError, TypeError) suppressed", exc_info=True
                )
        return 0.0

    def _calc_trend(self, dimension: str, days: int) -> str:
        """
        计算某维度的趋势

        策略：对比前半段时间和后半段时间的准确率
        """
        records = self._get_records_with_any_feedback(days)

        # 筛选包含该维度且有有效反馈的记录
        dim_records = [
            r for r in records if dimension in r.mirror_dimensions and self._has_any_feedback(r)
        ]

        if len(dim_records) < 6:
            return "insufficient_data"

        # 按时间排序
        dim_records.sort(key=lambda r: r.created_at)

        mid = len(dim_records) // 2
        first_half = dim_records[:mid]
        second_half = dim_records[mid:]

        def calc_rate(records):
            total = len(records)
            if total == 0:
                return 0.0
            positive = sum(1 for r in records if self._is_positive(self._get_effective_feedback(r)))
            return positive / total

        first_rate = calc_rate(first_half)
        second_rate = calc_rate(second_half)

        if second_rate > first_rate + 0.15:
            return "improving"
        elif second_rate < first_rate - 0.15:
            return "declining"
        return "stable"
