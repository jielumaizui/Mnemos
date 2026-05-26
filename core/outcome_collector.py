"""
OutcomeCollector — 结果收集器

【E14 全库修复】设计草案占位模块。
收集蒸馏和评分的结果，用于后续分析。
"""
from typing import List, Dict


class OutcomeCollector:
    """收集并汇总系统产出结果（设计草案，待完善）"""

    def __init__(self):
        self.outcomes: List[Dict] = []

    def record(self, outcome: Dict):
        """记录一个结果"""
        self.outcomes.append(outcome)

    def get_summary(self) -> Dict:
        """获取结果汇总"""
        return {"total": len(self.outcomes)}
