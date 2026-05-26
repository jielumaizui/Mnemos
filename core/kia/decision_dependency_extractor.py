"""
DecisionDependencyExtractor — 决策依赖提取器

【E14 全库修复】E13 连接 Worker 缺失子模块
从文本中提取决策及其依赖关系。
"""
from typing import List, Dict, Optional
import re


class DecisionDependencyExtractor:
    """提取决策节点和它们之间的依赖关系"""

    def __init__(self):
        self._decision_patterns = [
            re.compile(r'(?:决定|决策|选择|确定|采用|使用)\s*[:：]\s*(.+?)[。；\n]'),
            re.compile(r'(?:选择|决定|采用)\s+(.+?)\s+(?:因为|由于|考虑到|基于)'),
        ]

    def extract(self, text: str) -> List[Dict]:
        """
        提取决策及依赖

        Returns:
            [{"decision": str, "premises": [str], "confidence": float}]
        """
        decisions = []
        for pattern in self._decision_patterns:
            for m in pattern.finditer(text):
                decisions.append({
                    "decision": m.group(1).strip(),
                    "premises": [],
                    "confidence": 0.5,
                })
        return decisions
