"""
Time Parser - 时间解析器

解析会话中的时间信息，确定任务执行窗口。
支持中文/英文相对时间、周期性检测。
"""
# Kairos — 时机之神 — 时间解析，恰当时机的判定
# 原模块: time_parser.py



import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import List, Dict, Optional
from enum import Enum


class TimeWindowType(Enum):
    """时间窗口类型"""
    IMMEDIATE = "immediate"     # 即时（今天/马上）
    SHORT = "short"             # 短期（<=7天）
    MEDIUM = "medium"           # 中期（8-30天）
    LONG = "long"               # 长期（>30天）
    PERIODIC = "periodic"       # 周期性
    UNKNOWN = "unknown"         # 无法确定


@dataclass
class TimeWindow:
    """时间窗口"""
    window: TimeWindowType
    days_until: int             # 距离执行的天数（0=今天）
    due_date: Optional[datetime] = None  # 预估执行日期
    is_periodic: bool = False
    period: Optional[str] = None        # weekly/biweekly/monthly/quarterly
    periodic_keywords: List[str] = None  # 检测到的周期性关键词

    def __post_init__(self):
        if self.periodic_keywords is None:
            self.periodic_keywords = []


class TimeParser:
    """时间解析器"""

    # 相对时间模式 → 天数偏移
    RELATIVE_PATTERNS = [
        # 即时
        (r'现在|马上|立即|这就|right now|immediately|asap', 0),
        # 今天
        (r'今天|今晚|今天下午|今天晚上|today|tonight', 0),
        # 明天
        (r'明天|明早|明晚|tomorrow', 1),
        # 后天
        (r'后天|the day after tomorrow', 2),
        # 3天后
        (r'3天后|三天后|in 3 days', 3),
        # 本周内
        (r'本周|这周|this week', 3),
        # 下周
        (r'下周|下星期|next week', 7),
        # 下下周
        (r'下下周|the week after next', 14),
        # 本月底
        (r'本月|这个月|this month', 15),
        # 下个月
        (r'下个月|下月|next month', 30),
        # 下个月初
        (r'下个月初|下月初|early next month', 35),
        # 下个月底
        (r'下个月底|下月底|end of next month', 55),
        # 明年Q1
        (r'明年Q1|明年一季度|next Q1', 90),
        # 明年Q2
        (r'明年Q2|明年二季度|next Q2', 180),
        # 明年Q3
        (r'明年Q3|明年三季度|next Q3', 270),
        # 明年Q4
        (r'明年Q4|明年四季度|next Q4', 360),
        # 明年
        (r'明年|next year', 365),
    ]

    # 周期性模式
    PERIODIC_PATTERNS = [
        (r'每周|每星期|每周一|每周二|每周三|每周四|每周五|weekly|every week', 'weekly'),
        (r'每两周|双周|biweekly|fortnightly|every two weeks', 'biweekly'),
        (r'每月|每个月|monthly|every month', 'monthly'),
        (r'每季度|每季|quarterly|every quarter', 'quarterly'),
        (r'每年|每年一次|yearly|annually|every year', 'yearly'),
        (r'每天|每日|daily|every day', 'daily'),
    ]

    # 模糊时间
    FUZZY_PATTERNS = [
        (r'尽快|as soon as possible|尽快处理', 2),      # 2天内
        (r'有空时|有空|when you have time', 7),         # 一周内
        (r'不急|不着急|not urgent', 14),                # 两周内
    ]

    def __init__(self, reference_time: Optional[datetime] = None):
        self.reference_time = reference_time or datetime.now()

    def parse(self, content: str) -> TimeWindow:
        """
        解析内容中的时间信息

        Args:
            content: 用户消息内容

        Returns:
            TimeWindow 对象
        """
        # 1. 先检测周期性
        periodic_match = self._detect_periodic(content)
        if periodic_match:
            return periodic_match

        # 2. 检测相对时间
        relative_match = self._detect_relative(content)
        if relative_match:
            return relative_match

        # 3. 检测模糊时间
        fuzzy_match = self._detect_fuzzy(content)
        if fuzzy_match:
            return fuzzy_match

        # 4. 检测具体日期
        date_match = self._detect_date(content)
        if date_match:
            return date_match

        # 无法确定，默认即时
        return TimeWindow(
            window=TimeWindowType.IMMEDIATE,
            days_until=0,
            due_date=self.reference_time
        )

    def _detect_periodic(self, content: str) -> Optional[TimeWindow]:
        """检测周期性任务"""
        content_lower = content.lower()
        for pattern, period in self.PERIODIC_PATTERNS:
            if re.search(pattern, content_lower):
                return TimeWindow(
                    window=TimeWindowType.PERIODIC,
                    days_until=0,
                    due_date=self.reference_time,
                    is_periodic=True,
                    period=period,
                    periodic_keywords=[period]
                )
        return None

    def _detect_relative(self, content: str) -> Optional[TimeWindow]:
        """检测相对时间"""
        content_lower = content.lower()
        for pattern, days in self.RELATIVE_PATTERNS:
            if re.search(pattern, content_lower):
                due_date = self.reference_time + timedelta(days=days)
                window_type = self._days_to_window(days)
                return TimeWindow(
                    window=window_type,
                    days_until=days,
                    due_date=due_date
                )
        return None

    def _detect_fuzzy(self, content: str) -> Optional[TimeWindow]:
        """检测模糊时间"""
        content_lower = content.lower()
        for pattern, days in self.FUZZY_PATTERNS:
            if re.search(pattern, content_lower):
                due_date = self.reference_time + timedelta(days=days)
                window_type = self._days_to_window(days)
                return TimeWindow(
                    window=window_type,
                    days_until=days,
                    due_date=due_date
                )
        return None

    def _detect_date(self, content: str) -> Optional[TimeWindow]:
        """检测具体日期格式"""
        # 格式：2026-05-07 或 2026/05/07
        date_patterns = [
            r'(\d{4})[-/](\d{1,2})[-/](\d{1,2})',
            r'(\d{4})年(\d{1,2})月(\d{1,2})日',
        ]
        for pattern in date_patterns:
            match = re.search(pattern, content)
            if match:
                try:
                    year, month, day = int(match.group(1)), int(match.group(2)), int(match.group(3))
                    due_date = datetime(year, month, day)
                    days = (due_date - self.reference_time).days
                    if days >= 0:
                        window_type = self._days_to_window(days)
                        return TimeWindow(
                            window=window_type,
                            days_until=days,
                            due_date=due_date
                        )
                except ValueError:
                    continue
        return None

    def _days_to_window(self, days: int) -> TimeWindowType:
        """天数转换为窗口类型"""
        if days <= 1:
            return TimeWindowType.IMMEDIATE
        elif days <= 7:
            return TimeWindowType.SHORT
        elif days <= 30:
            return TimeWindowType.MEDIUM
        else:
            return TimeWindowType.LONG

    def should_load_now(self, time_window: TimeWindow) -> bool:
        """
        判断是否应该立即装载知识

        规则：
        - 即时/短期：立即装载
        - 中期：不装载，记入调度器
        - 长期：不装载，记入调度器
        - 周期性：检查上次执行时间，如果已过期则装载
        """
        if time_window.window in (TimeWindowType.IMMEDIATE, TimeWindowType.SHORT):
            return True
        if time_window.is_periodic:
            return True  # 周期性任务在触发时立即装载
        return False

    def get_reminder_days_before(self, time_window: TimeWindow) -> int:
        """获取提前提醒天数"""
        if time_window.window == TimeWindowType.MEDIUM:
            return 3
        elif time_window.window == TimeWindowType.LONG:
            return 7
        return 0


class PeriodicDetector:
    """周期性检测器 - 基于历史记录"""

    def detect(self, task_type: str, history: List[Dict]) -> Optional[str]:
        """
        检测同一类型任务是否在规律间隔出现

        Args:
            task_type: 任务类型
            history: 历史记录列表，每项包含 'created_at' 和 'task_type'

        Returns:
            weekly/biweekly/monthly/quarterly/yearly/None
        """
        # 筛选同类型任务
        dates = []
        for h in history:
            if h.get('task_type') == task_type and h.get('created_at'):
                try:
                    dt = datetime.fromisoformat(h['created_at'].replace('Z', '+00:00'))
                    dates.append(dt)
                except (ValueError, AttributeError):
                    continue

        if len(dates) < 3:
            return None

        # 按时间排序
        dates.sort()

        # 计算间隔（天）
        intervals = []
        for i in range(len(dates) - 1):
            delta = (dates[i + 1] - dates[i]).days
            if delta > 0:
                intervals.append(delta)

        if len(intervals) < 2:
            return None

        # 使用加权滑动窗口（最近间隔权重更高）
        weighted_avg = self._weighted_average(intervals)
        variance = self._variance(intervals, weighted_avg)

        # 方差容忍度：间隔平均值的30%
        threshold = weighted_avg * 0.3

        if variance > threshold:
            return None  # 间隔不稳定，不是周期性任务

        # 判断周期
        if 6 <= weighted_avg <= 8:
            return 'weekly'
        elif 13 <= weighted_avg <= 15:
            return 'biweekly'
        elif 28 <= weighted_avg <= 31:
            return 'monthly'
        elif 85 <= weighted_avg <= 95:
            return 'quarterly'
        elif 360 <= weighted_avg <= 370:
            return 'yearly'

        return None

    def _weighted_average(self, intervals: List[int]) -> float:
        """加权平均（最近间隔权重更高）"""
        if not intervals:
            return 0.0
        total = 0
        weight_sum = 0
        for i, val in enumerate(intervals):
            weight = i + 1  # 越新的权重越高
            total += val * weight
            weight_sum += weight
        return total / weight_sum if weight_sum > 0 else 0.0

    def _variance(self, intervals: List[int], mean: float) -> float:
        """计算方差"""
        if not intervals:
            return 0.0
        return sum((x - mean) ** 2 for x in intervals) / len(intervals)


# ========== 便捷函数 ==========

def parse_time(content: str, reference_time: Optional[datetime] = None) -> TimeWindow:
    """便捷函数：解析时间"""
    parser = TimeParser(reference_time=reference_time)
    return parser.parse(content)


def should_load_knowledge(content: str) -> tuple:
    """
    便捷函数：判断是否应该立即装载知识

    Returns:
        (should_load: bool, time_window: TimeWindow)
    """
    parser = TimeParser()
    tw = parser.parse(content)
    return parser.should_load_now(tw), tw
