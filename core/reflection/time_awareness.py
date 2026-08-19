"""
时间感知模块 — 代码层实现

核心原则：不依赖 Agent "天然"的时间理解，所有时间感知逻辑写在代码里。

功能：
1. 时间衰减权重：离现在越远的 Observation，权重越低
2. 时间节律检测：当前处于什么阶段（年初、季度末、深夜等）
3. 时间跨度理解：30天、6个月、2年对人类的不同意义
4. 上次分析间隔：距离上次 Reflection 多久

使用方式：
    ta = TimeAwareness(observation_store, reflection_store)
    context = ta.get_temporal_context()
    # context 包含：当前时间、时间节律、上次分析间隔、各维度数据新鲜度
"""

import logging
import math
from dataclasses import dataclass, field
from datetime import datetime
from typing import Dict, Optional

from core.access_policy import AccessNarrowing, PrincipalEnvelope

# Constants extracted from magic numbers
TIME_AWARENESS_DURATION_BUCKET_MONTH_DAYS = 30
TIME_AWARENESS_DURATION_BUCKET_WEEK_DAYS = 7
TIME_AWARENESS_DURATION_BUCKET_QUARTER_DAYS = 90
TIME_AWARENESS_DURATION_BUCKET_HALF_YEAR_DAYS = 180
TIME_AWARENESS_DURATION_BUCKET_YEAR_DAYS = 365
TIME_AWARENESS__DETECT_RHYTHM_DAYS = 7

logger = logging.getLogger(__name__)


# 时间跨度语义定义（天数 → 人类感知）
DURATION_SEMANTICS = {
    "very_recent": (0, 7),  # 最近一周 — "刚发生的事"
    "recent": (7, 30),  # 一个月内 — "最近"
    "moderate": (30, 90),  # 1-3个月 — "前段时间"
    "distant": (90, 365),  # 3-12个月 — "去年/早些时候"
    "far": (365, 730),  # 1-2年 — "一两年前"
    "very_far": (730, float("inf")),  # 2年以上 — "很久以前"
}

# 时间半衰期配置（按维度）
# 不同维度的 Observation "过期速度"不同
DIMENSION_HALF_LIFE = {
    "attention": 45,  # 关注变化较快，45天半衰期
    "decisions": 90,  # 决策模式变化较慢
    "actions": 30,  # 行动模式变化快
    "time": 180,  # 时间估算偏差相对稳定
    "stress": 14,  # 压力信号变化很快
    "relationships": 60,  # 关系模式中等速度
    "growth": 365,  # 成长轨迹变化最慢
}

# 时间节律定义
RHYTHM_PERIODS = {
    "year_start": ((1, 1), (1, 31)),  # 年初（制定计划期）
    "year_end": ((12, 1), (12, 31)),  # 年末（复盘期）
    "quarter_start": None,  # 动态计算
    "quarter_end": None,  # 动态计算
    "weekend": None,  # 动态计算
    "late_night": ((0, 0), (6, 0)),  # 凌晨（判断力下降）
}


def _in_configured_period(point: tuple[int, int], period: Optional[tuple]) -> bool:
    """Return whether a month/day or hour/minute point is inside a configured range."""
    if period is None:
        return False
    start, end = period
    if start <= end:
        return bool(start <= point <= end)
    return bool(point >= start or point <= end)


@dataclass
class TemporalContext:
    """时间上下文 — 供 Mirror/Insight 使用"""

    now: datetime
    now_str: str  # 格式化时间字符串

    # 时间节律
    rhythm: str  # 当前处于什么节律（如 "year_start", "normal"）
    rhythm_description: str  # 节律描述（如 "年初，通常是制定计划的高峰期"）

    # 上次 Reflection 间隔
    last_reflection_ago: Optional[int] = None  # 距离上次 Reflection 的天数
    last_reflection_trigger: Optional[str] = None

    # 各维度数据新鲜度
    dimension_freshness: Dict[str, Dict] = field(default_factory=dict)
    # {dimension: {"latest_observation_days_ago": N, "status": "fresh|stale|expired"}}

    # 时间跨度语义（用于 Insight 生成时的措辞）
    duration_semantics: Dict[str, str] = field(default_factory=dict)


class TimeAwareness:
    """时间感知引擎"""

    def __init__(self, observation_store=None, reflection_store=None):
        self.obs_store = observation_store
        self.ref_store = reflection_store

    def get_temporal_context(
        self,
        as_of: Optional[datetime] = None,
        *,
        principal: PrincipalEnvelope | None = None,
        narrowing: AccessNarrowing | None = None,
    ) -> TemporalContext:
        """获取完整的时间上下文"""
        now = as_of or datetime.now()

        # 1. 检测时间节律
        rhythm, rhythm_desc = self._detect_rhythm(now)

        # 2. 检测上次 Reflection 间隔
        last_reflection_ago, last_trigger = self._last_reflection_info(
            principal=principal,
            narrowing=narrowing,
        )

        # 3. 检测各维度数据新鲜度
        dim_freshness = self._dimension_freshness(
            now,
            principal=principal,
            narrowing=narrowing,
        )

        # 4. 生成时间跨度语义
        duration_semantics = self._build_duration_semantics(now)

        return TemporalContext(
            now=now,
            now_str=now.strftime("%Y-%m-%d %H:%M"),
            rhythm=rhythm,
            rhythm_description=rhythm_desc,
            last_reflection_ago=last_reflection_ago,
            last_reflection_trigger=last_trigger,
            dimension_freshness=dim_freshness,
            duration_semantics=duration_semantics,
        )

    def recency_weight(
        self, period_end: Optional[datetime], dimension: str, as_of: Optional[datetime] = None
    ) -> float:
        """
        计算时间衰减权重

        公式：weight = exp(-days_ago / half_life)

        Args:
            period_end: Observation 的统计周期结束时间
            dimension: 维度名称（用于选择半衰期）
            as_of: 参考时间点（默认 now）

        Returns:
            float: 0.0-1.0 的权重值
        """
        if not period_end:
            return 1.0

        ref = as_of or datetime.now()
        days_ago = (ref - period_end).days

        if days_ago < 0:
            return 1.0  # 未来的数据，权重最大

        half_life = DIMENSION_HALF_LIFE.get(dimension, TIME_AWARENESS_DURATION_BUCKET_MONTH_DAYS)
        weight = math.exp(-days_ago / half_life)

        # 平滑处理：最近7天保持高权重
        if days_ago <= TIME_AWARENESS_DURATION_BUCKET_WEEK_DAYS:
            weight = max(weight, 0.9)

        return round(weight, 3)

    def humanize_duration(self, days: int) -> str:
        """
        将天数转换为人类可理解的时间描述

        代码层实现，不依赖 Agent 推理
        """
        if days <= TIME_AWARENESS_DURATION_BUCKET_WEEK_DAYS:
            return "最近"
        elif days <= TIME_AWARENESS_DURATION_BUCKET_MONTH_DAYS:
            return "一个月左右"
        elif days <= TIME_AWARENESS_DURATION_BUCKET_QUARTER_DAYS:
            return "几个月前"
        elif days <= TIME_AWARENESS_DURATION_BUCKET_HALF_YEAR_DAYS:
            return "半年前"
        elif days <= TIME_AWARENESS_DURATION_BUCKET_YEAR_DAYS:
            return "去年"
        elif days <= 730:
            return "一两年前"
        else:
            return "很久以前"

    def freshness_status(self, days_ago: int, dimension: str) -> str:
        """
        判断数据新鲜度状态

        Returns: "fresh" | "stale" | "expired"
        """
        half_life = DIMENSION_HALF_LIFE.get(dimension, TIME_AWARENESS_DURATION_BUCKET_MONTH_DAYS)

        # 1个半衰期内：新鲜
        if days_ago <= half_life:
            return "fresh"
        # 2-3个半衰期：陈旧
        elif days_ago <= half_life * 3:
            return "stale"
        # 超过3个半衰期：过期
        else:
            return "expired"

    def _detect_rhythm(self, now: datetime) -> tuple:
        """检测当前时间节律"""
        month, day = now.month, now.day
        weekday = now.weekday()  # 0=周一, 6=周日
        hour = now.hour

        # 年初
        if _in_configured_period((month, day), RHYTHM_PERIODS.get("year_start")):
            return "year_start", "年初，通常是制定计划、设定目标的高峰期"

        # 年末
        if _in_configured_period((month, day), RHYTHM_PERIODS.get("year_end")):
            return "year_end", "年末，通常是复盘、总结、调整方向的时间"

        # 季度末（3月、6月、9月、12月的最后一周）
        if month in [3, 6, 9, 12] and day >= 24:
            quarter_names = {3: "一季度", 6: "上半年", 9: "三季度", 12: "全年"}
            return "quarter_end", f"{quarter_names[month]}尾声，通常有阶段性复盘压力"

        # 季度初
        if (
            month in [1, 4, TIME_AWARENESS__DETECT_RHYTHM_DAYS, 10]
            and day <= TIME_AWARENESS__DETECT_RHYTHM_DAYS
        ):
            return "quarter_start", "季度初，通常是启动新计划的时间"

        # 周末
        if weekday >= 5:
            return "weekend", "周末，可能有不同的生活节奏和关注点"

        # 深夜
        if _in_configured_period((hour, now.minute), RHYTHM_PERIODS.get("late_night")):
            return "late_night", "深夜，注意力和判断力可能下降"

        # 工作日
        if weekday < 5:
            if hour < 12:
                return "workday_morning", "工作日上午，通常是高效处理事务的时间"
            elif hour < 14:
                return "workday_noon", "午休时间"
            else:
                return "workday_afternoon", "工作日下午"

        return "normal", "常规时间"

    def _last_reflection_info(
        self,
        *,
        principal: PrincipalEnvelope | None,
        narrowing: AccessNarrowing | None,
    ) -> tuple:
        """获取上次 Reflection 的信息"""
        if not self.ref_store:
            return None, None

        try:
            latest, _summary = self.ref_store.authorized_get_latest(
                principal=principal,
                narrowing=narrowing,
                purpose="reflection_prompt",
                limit=1,
            )
            if latest:
                record = latest[0]
                days_ago = (datetime.now() - record.created_at).days
                return days_ago, record.trigger.value
        except (OSError, ValueError, TypeError, KeyError, ImportError, AttributeError, RuntimeError):
            logger.warning("获取上次 Reflection 信息失败", exc_info=True)

        return None, None

    def _dimension_freshness(
        self,
        now: datetime,
        *,
        principal: PrincipalEnvelope | None,
        narrowing: AccessNarrowing | None,
    ) -> Dict[str, Dict]:
        """检测各维度数据新鲜度"""
        if not self.obs_store:
            return {}

        result = {}
        from core.cognitive.models import Dimension

        for dim in Dimension:
            try:
                latest, _summary = self.obs_store.authorized_query(
                    principal=principal,
                    narrowing=narrowing,
                    purpose="reflection_prompt",
                    dimension=dim,
                    limit=1,
                )
                if latest and latest[0].period_end:
                    days_ago = (now - latest[0].period_end).days
                    status = self.freshness_status(days_ago, dim.value)
                    result[dim.value] = {
                        "latest_observation_days_ago": days_ago,
                        "status": status,
                        "humanized": self.humanize_duration(days_ago),
                    }
                else:
                    result[dim.value] = {
                        "latest_observation_days_ago": None,
                        "status": "no_data",
                        "humanized": "暂无数据",
                    }
            except (OSError, ValueError, TypeError, KeyError, ImportError, AttributeError, RuntimeError):
                result[dim.value] = {
                    "latest_observation_days_ago": None,
                    "status": "error",
                    "humanized": "检测失败",
                }

        return result

    def _build_duration_semantics(self, now: datetime) -> Dict[str, str]:
        """构建常用时间跨度语义"""
        boundary_days = {
            TIME_AWARENESS_DURATION_BUCKET_WEEK_DAYS,
            TIME_AWARENESS_DURATION_BUCKET_MONTH_DAYS,
            TIME_AWARENESS_DURATION_BUCKET_QUARTER_DAYS,
            TIME_AWARENESS_DURATION_BUCKET_HALF_YEAR_DAYS,
            TIME_AWARENESS_DURATION_BUCKET_YEAR_DAYS,
        }
        for _semantic, (_start_day, end_day) in DURATION_SEMANTICS.items():
            if math.isfinite(end_day):
                boundary_days.add(int(end_day))

        return {
            f"{days}d": self.humanize_duration(days)
            for days in sorted(boundary_days)
        }
