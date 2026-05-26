"""
CrossAgentDivergenceDetector — 跨 Agent 分歧检测器

【E14 全库修复】A8 跨 Agent 知识关联完整实现。
检测不同 Agent 对同一知识点的处理是否存在分歧。
"""

import re
import math
from typing import List, Dict, Optional, Tuple
from dataclasses import dataclass
from collections import Counter


@dataclass
class DivergenceReport:
    """分歧报告"""
    topic: str
    divergence_score: float       # 0.0-1.0，越高分歧越大
    confidence_variance: float    # 置信度方差
    semantic_similarity: float    # 语义相似度
    agent_outputs: List[Dict]
    severity: str                 # minor / moderate / severe
    reason: str


class CrossAgentDivergenceDetector:
    """检测跨 Agent 知识处理分歧"""

    def __init__(self, similarity_threshold: float = 0.6,
                 variance_threshold: float = 0.15):
        self.similarity_threshold = similarity_threshold
        self.variance_threshold = variance_threshold

    def detect_divergence(self, agent_outputs: List[Dict]) -> List[DivergenceReport]:
        """
        检测多个 Agent 对同一输入的输出分歧

        Args:
            agent_outputs: [
                {"agent_id": str, "output": str, "confidence": float, "topic": str}
            ]

        Returns:
            分歧报告列表
        """
        if len(agent_outputs) < 2:
            return []

        # 按 topic 分组
        topic_groups = {}
        for out in agent_outputs:
            topic = out.get("topic", "general")
            if topic not in topic_groups:
                topic_groups[topic] = []
            topic_groups[topic].append(out)

        reports = []
        for topic, outputs in topic_groups.items():
            if len(outputs) < 2:
                continue

            report = self._analyze_topic_divergence(topic, outputs)
            if report and report.divergence_score > 0.3:
                reports.append(report)

        return sorted(reports, key=lambda r: r.divergence_score, reverse=True)

    def compute_divergence_score(self, outputs: List[str]) -> float:
        """计算输出列表的分歧分数 0.0-1.0"""
        if len(outputs) < 2:
            return 0.0

        # 两两计算语义相似度，取平均差异
        similarities = []
        for i in range(len(outputs)):
            for j in range(i + 1, len(outputs)):
                sim = self._semantic_similarity(outputs[i], outputs[j])
                similarities.append(sim)

        avg_similarity = sum(similarities) / len(similarities) if similarities else 1.0
        # 分歧分数 = 1 - 平均相似度
        return round(1.0 - avg_similarity, 3)

    def _analyze_topic_divergence(self, topic: str,
                                  outputs: List[Dict]) -> Optional[DivergenceReport]:
        """分析单个主题的分歧情况"""
        texts = [o.get("output", "") for o in outputs]
        confidences = [o.get("confidence", 0.5) for o in outputs]

        # 1. 语义相似度分析
        pairwise_sims = []
        for i in range(len(texts)):
            for j in range(i + 1, len(texts)):
                sim = self._semantic_similarity(texts[i], texts[j])
                pairwise_sims.append(sim)

        avg_similarity = sum(pairwise_sims) / len(pairwise_sims) if pairwise_sims else 1.0
        min_similarity = min(pairwise_sims) if pairwise_sims else 1.0

        # 2. 置信度方差分析
        if len(confidences) >= 2:
            mean_conf = sum(confidences) / len(confidences)
            variance = sum((c - mean_conf) ** 2 for c in confidences) / len(confidences)
            std_dev = math.sqrt(variance)
        else:
            std_dev = 0.0
            mean_conf = confidences[0] if confidences else 0.5

        # 3. 计算综合分歧分数
        # 语义差异权重 0.6，置信度差异权重 0.4
        semantic_component = 1.0 - avg_similarity
        confidence_component = min(1.0, std_dev * 3)  # 放大方差影响
        divergence_score = semantic_component * 0.6 + confidence_component * 0.4

        # 4. 判定严重程度
        if divergence_score >= 0.7:
            severity = "severe"
        elif divergence_score >= 0.4:
            severity = "moderate"
        elif divergence_score >= 0.2:
            severity = "minor"
        else:
            severity = "none"

        if severity == "none":
            return None

        # 5. 生成原因描述
        reasons = []
        if semantic_component > 0.3:
            reasons.append(f"语义相似度低（平均 {avg_similarity:.2f}，最低 {min_similarity:.2f}）")
        if confidence_component > 0.3:
            reasons.append(f"置信度差异大（标准差 {std_dev:.3f}，范围 {min(confidences):.2f}-{max(confidences):.2f}）")

        return DivergenceReport(
            topic=topic,
            divergence_score=round(divergence_score, 3),
            confidence_variance=round(std_dev ** 2, 4),
            semantic_similarity=round(avg_similarity, 3),
            agent_outputs=outputs,
            severity=severity,
            reason="; ".join(reasons) if reasons else f"检测到 {severity} 级别分歧",
        )

    def _semantic_similarity(self, text1: str, text2: str) -> float:
        """
        计算两段文本的语义相似度（词袋 Jaccard + 关键词重叠）

        零 API 成本实现。
        """
        if not text1 or not text2:
            return 0.0

        # 提取词
        words1 = set(self._extract_meaningful_words(text1))
        words2 = set(self._extract_meaningful_words(text2))

        if not words1 or not words2:
            return 0.0

        intersection = words1 & words2
        union = words1 | words2
        jaccard = len(intersection) / len(union)

        # 实体匹配加分
        entities1 = set(self._extract_entities(text1))
        entities2 = set(self._extract_entities(text2))
        if entities1 and entities2:
            entity_overlap = len(entities1 & entities2) / max(len(entities1), len(entities2))
            jaccard = jaccard * 0.7 + entity_overlap * 0.3

        return min(1.0, jaccard)

    def _extract_meaningful_words(self, text: str) -> List[str]:
        """提取有意义的词"""
        zh = re.findall(r'[\u4e00-\u9fa5]{2,}', text)
        en = re.findall(r'[a-zA-Z]{3,}', text.lower())
        # 过滤停用词
        stopwords = {'the', 'and', 'for', 'are', 'but', 'not', 'you', 'all', 'can',
                     'had', 'her', 'was', 'one', 'our', 'out', 'day', 'get', 'has',
                     'him', 'his', 'man', 'new', 'now', 'old', 'see', 'two', 'way',
                     'who', 'boy', 'did', 'its', 'let', 'put', 'say', 'she', 'too',
                     'use', 'that', 'with', 'have', 'this', 'will', 'your', 'from',
                     'they', 'know', 'want', 'been', 'good', 'much', 'some', 'time'}
        en = [w for w in en if w not in stopwords]
        return zh + en

    def _extract_entities(self, text: str) -> List[str]:
        """提取技术实体"""
        # CamelCase
        camel = re.findall(r'\b[A-Z][a-z]+[A-Z]\w+\b', text)
        # 缩写
        acronyms = re.findall(r'\b[A-Z]{2,10}\b', text)
        # 中文技术实体
        zh_entities = re.findall(r'[\u4e00-\u9fa5]{2,8}(?:技术|框架|系统|工具|语言|库|引擎)', text)
        return [e.lower() for e in camel + acronyms + zh_entities]

    def get_divergence_summary(self, reports: List[DivergenceReport]) -> Dict:
        """获取分歧汇总"""
        if not reports:
            return {"total": 0, "severe": 0, "moderate": 0, "minor": 0, "avg_score": 0.0}

        severe = sum(1 for r in reports if r.severity == "severe")
        moderate = sum(1 for r in reports if r.severity == "moderate")
        minor = sum(1 for r in reports if r.severity == "minor")

        return {
            "total": len(reports),
            "severe": severe,
            "moderate": moderate,
            "minor": minor,
            "avg_score": round(sum(r.divergence_score for r in reports) / len(reports), 3),
            "highest_divergence_topic": reports[0].topic if reports else None,
        }
