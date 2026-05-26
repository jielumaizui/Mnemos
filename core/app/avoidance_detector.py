"""
AvoidanceDetector — 盲区回避检测器

【E14 全库修复】A3 盲区主动发现完整实现。
检测用户是否习惯性地回避某些话题/知识点：
- 主题共现频率统计
- 点击率对比分析
- 显著性阈值判定
"""

import re
import math
from typing import List, Dict, Optional, Tuple
from collections import Counter, defaultdict
from dataclasses import dataclass


@dataclass
class AvoidancePattern:
    """回避模式"""
    topic: str
    occurrence_count: int
    click_count: int
    click_rate: float
    avg_click_rate: float
    significance: float
    confidence: float
    reason: str


class AvoidanceDetector:
    """检测用户回避模式：某些主题反复出现但被忽略"""

    def __init__(self, min_occurrences: int = 3, lookback_days: int = 30,
                 significance_threshold: float = 2.0):
        self.min_occurrences = min_occurrences
        self.lookback_days = lookback_days
        self.significance_threshold = significance_threshold
        self.detected_patterns: List[AvoidancePattern] = []

    def analyze(self, query_history: List[Dict]) -> List[AvoidancePattern]:
        """
        分析查询历史，检测回避模式

        Args:
            query_history: [
                {"query": str, "timestamp": str, "clicked_results": [str],
                 "results_shown": [str]}
            ]

        Returns:
            检测到的回避模式列表
        """
        if not query_history or len(query_history) < self.min_occurrences:
            return []

        # 1. 提取所有主题
        topic_occurrences = self._extract_topics(query_history)

        # 2. 计算全局平均点击率
        global_click_rate = self._compute_global_click_rate(query_history)

        # 3. 对每个主题进行回避检测
        patterns = []
        for topic, entries in topic_occurrences.items():
            if len(entries) < self.min_occurrences:
                continue

            pattern = self._analyze_topic(topic, entries, global_click_rate)
            if pattern:
                patterns.append(pattern)

        # 按显著性排序
        patterns.sort(key=lambda p: p.significance, reverse=True)
        self.detected_patterns = patterns
        return patterns

    def get_avoidance_score(self, topic: str, history: List[Dict]) -> float:
        """计算特定主题的回避分数 0.0-1.0"""
        topic_entries = []
        for entry in history:
            if topic.lower() in entry.get("query", "").lower():
                topic_entries.append(entry)

        if len(topic_entries) < self.min_occurrences:
            return 0.0

        global_click_rate = self._compute_global_click_rate(history)
        pattern = self._analyze_topic(topic, topic_entries, global_click_rate)
        if pattern:
            return min(1.0, pattern.significance / 5.0)
        return 0.0

    def _extract_topics(self, query_history: List[Dict]) -> Dict[str, List[Dict]]:
        """从查询历史中提取主题"""
        topic_map = defaultdict(list)

        for entry in query_history:
            query = entry.get("query", "")
            # 提取关键词作为主题
            topics = self._extract_keywords(query)
            for topic in topics:
                topic_map[topic].append(entry)

        return dict(topic_map)

    def _extract_keywords(self, text: str) -> List[str]:
        """提取主题关键词（名词短语）"""
        # 中文技术概念
        zh_concepts = re.findall(r'[\u4e00-\u9fa5]{2,6}(?:技术|方法|框架|系统|工具|问题|方案|优化|设计|架构|模式)', text)
        # 英文技术术语
        en_terms = re.findall(r'\b[A-Z][a-zA-Z]*(?:\s+[A-Z][a-zA-Z]*){0,2}\b', text)
        en_terms = [t for t in en_terms if len(t) > 3 and t.lower() not in {
            'what', 'how', 'why', 'when', 'where', 'which', 'this', 'that', 'with', 'from',
            'the', 'and', 'for', 'are', 'but', 'not', 'you', 'all', 'can', 'had', 'her',
            'was', 'one', 'our', 'out', 'day', 'get', 'has', 'him', 'his', 'man', 'new',
        }]

        return [c.lower() for c in zh_concepts + en_terms]

    def _compute_global_click_rate(self, history: List[Dict]) -> float:
        """计算全局平均点击率"""
        total_shown = 0
        total_clicked = 0

        for entry in history:
            shown = len(entry.get("results_shown", []))
            clicked = len(entry.get("clicked_results", []))
            total_shown += shown
            total_clicked += clicked

        if total_shown == 0:
            return 0.3  # 默认点击率

        return total_clicked / total_shown

    def _analyze_topic(self, topic: str, entries: List[Dict],
                       global_click_rate: float) -> Optional[AvoidancePattern]:
        """分析单个主题是否存在回避模式"""
        occurrence_count = len(entries)

        # 统计该主题的点击情况
        click_count = 0
        for entry in entries:
            clicked = entry.get("clicked_results", [])
            # 如果用户点击了与该主题相关的结果
            for result in clicked:
                if topic.lower() in result.lower():
                    click_count += 1
                    break

        click_rate = click_count / occurrence_count if occurrence_count > 0 else 0

        # 显著性检验：z-score
        # H0: 该主题的点击率 = 全局点击率
        # z = (p - P0) / sqrt(P0 * (1 - P0) / n)
        if global_click_rate > 0 and occurrence_count > 0:
            std_error = math.sqrt(global_click_rate * (1 - global_click_rate) / occurrence_count)
            if std_error > 0:
                z_score = (click_rate - global_click_rate) / std_error
            else:
                z_score = 0
        else:
            z_score = 0

        # 回避判定：点击率显著低于全局平均
        significance = abs(z_score) if click_rate < global_click_rate else 0

        if significance < self.significance_threshold:
            return None

        confidence = min(1.0, significance / 5.0)

        reason = (
            f"主题 '{topic}' 出现 {occurrence_count} 次，"
            f"但仅点击 {click_count} 次（点击率 {click_rate:.1%}），"
            f"显著低于全局平均 {global_click_rate:.1%} "
            f"（z-score: {z_score:.2f}）"
        )

        return AvoidancePattern(
            topic=topic,
            occurrence_count=occurrence_count,
            click_count=click_count,
            click_rate=click_rate,
            avg_click_rate=global_click_rate,
            significance=significance,
            confidence=confidence,
            reason=reason,
        )

    def get_avoidance_summary(self) -> Dict:
        """获取回避检测汇总"""
        if not self.detected_patterns:
            return {"detected": 0, "topics": [], "avg_confidence": 0.0}

        return {
            "detected": len(self.detected_patterns),
            "topics": [p.topic for p in self.detected_patterns],
            "avg_confidence": round(sum(p.confidence for p in self.detected_patterns) / len(self.detected_patterns), 3),
            "most_avoided": self.detected_patterns[0].topic if self.detected_patterns else None,
        }
