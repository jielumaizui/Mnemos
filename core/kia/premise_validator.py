"""
PremiseValidator — 前提验证器

【E14 全库修复】E3 影子页面完整实现。
验证决策前提是否仍然成立：语义匹配 + 时效性检查。
"""

import re
from typing import Dict, List, Optional, Tuple
from datetime import datetime


import logging
logger = logging.getLogger(__name__)
class PremiseValidator:
    """验证决策前提的当前有效性"""

    # 时间敏感词汇（提示前提可能随时间失效）
    TIME_SENSITIVE_KEYWORDS = {
        "当前", "目前", "现在", "暂时", "短期", "近期",
        "current", "present", "now", "temporary", "short-term",
        "2024", "2025", "2026",  # 具体年份通常有时效性
        "v1", "v2", "version", "beta", "alpha",  # 版本号可能变化
    }

    # 过时信号词汇（提示前提可能已经失效）
    OBSOLETE_SIGNALS = {
        "旧版", "老方法", "之前", "以前", "曾经",
        "deprecated", "legacy", "old", "previous", "former",
    }

    def __init__(self, similarity_threshold: float = 0.3,
                 time_sensitive_penalty: float = 0.2):
        self.similarity_threshold = similarity_threshold
        self.time_sensitive_penalty = time_sensitive_penalty

    def validate(self, premise: str, current_context: str,
                 premise_date: str = None) -> Dict:
        """
        验证前提在当前上下文中是否仍然成立

        Args:
            premise: 原始前提陈述
            current_context: 当前知识上下文
            premise_date: 前提建立时间（ISO格式，可选）

        Returns:
            {"valid": bool, "confidence": float, "reason": str, "checks": {}}
        """
        checks = {}

        # 1. 语义匹配检查
        semantic_score, semantic_reason = self._check_semantic_match(
            premise, current_context
        )
        checks["semantic"] = {
            "score": round(semantic_score, 3),
            "passed": semantic_score >= self.similarity_threshold,
            "reason": semantic_reason,
        }

        # 2. 时效性检查
        time_score, time_reason = self._check_timeliness(premise, premise_date)
        checks["timeliness"] = {
            "score": round(time_score, 3),
            "passed": time_score >= 0.5,
            "reason": time_reason,
        }

        # 3. 过时信号检查
        obsolete_score, obsolete_reason = self._check_obsolete_signals(premise, current_context)
        checks["obsolete"] = {
            "score": round(obsolete_score, 3),
            "passed": obsolete_score >= 0.5,
            "reason": obsolete_reason,
        }

        # 综合判定
        overall_score = (semantic_score * 0.5 +
                        time_score * 0.25 +
                        obsolete_score * 0.25)

        valid = (checks["semantic"]["passed"] and
                 checks["timeliness"]["passed"] and
                 checks["obsolete"]["passed"])

        if not valid:
            reasons = []
            if not checks["semantic"]["passed"]:
                reasons.append(f"语义匹配度低 ({semantic_score:.2f})")
            if not checks["timeliness"]["passed"]:
                reasons.append(f"时效性不足 ({time_reason})")
            if not checks["obsolete"]["passed"]:
                reasons.append(f"检测到过时信号 ({obsolete_reason})")
            reason = "; ".join(reasons)
        else:
            reason = f"前提有效（综合得分: {overall_score:.2f}）"

        return {
            "valid": valid,
            "confidence": round(overall_score, 3),
            "reason": reason,
            "checks": checks,
        }

    def batch_validate(self, premises: List[str], current_context: str,
                       premise_dates: List[str] = None) -> List[Dict]:
        """批量验证多个前提"""
        premise_dates = premise_dates or [None] * len(premises)
        return [self.validate(p, current_context, d)
                for p, d in zip(premises, premise_dates)]

    def _check_semantic_match(self, premise: str,
                              current_context: str) -> Tuple[float, str]:
        """语义匹配检查：Jaccard 相似度 + 关键词重叠"""
        # 提取关键词
        premise_words = set(self._extract_words(premise))
        context_words = set(self._extract_words(current_context))

        if not premise_words or not context_words:
            return 0.0, "无法提取有效关键词"

        intersection = premise_words & context_words
        union = premise_words | context_words

        jaccard = len(intersection) / len(union) if union else 0

        # 额外加分：如果核心概念词完全匹配
        core_concepts = self._extract_core_concepts(premise)
        matched_concepts = [c for c in core_concepts if c in current_context.lower()]
        concept_bonus = min(0.2, len(matched_concepts) * 0.05)

        score = min(1.0, jaccard + concept_bonus)

        if score >= self.similarity_threshold:
            return score, f"语义匹配度 {score:.2f}（Jaccard: {jaccard:.2f}, 概念匹配: {len(matched_concepts)}）"
        else:
            return score, f"语义匹配度不足 {score:.2f} < 阈值 {self.similarity_threshold}"

    def _check_timeliness(self, premise: str,
                          premise_date: str = None) -> Tuple[float, str]:
        """时效性检查"""
        premise_lower = premise.lower()

        # 1. 检测时间敏感词汇
        time_sensitive_count = sum(1 for kw in self.TIME_SENSITIVE_KEYWORDS
                                   if kw.lower() in premise_lower)

        if time_sensitive_count == 0:
            # 没有时间敏感词汇，默认高时效性
            return 1.0, "无时间敏感词汇"

        # 2. 如果有建立日期，检查距今多久
        age_penalty = 0.0
        if premise_date:
            try:
                pdate = datetime.fromisoformat(premise_date.replace('Z', '+00:00'))
                age_days = (datetime.now() - pdate).days
                # 时间敏感的前提，90天后开始扣分
                if age_days > 90:
                    age_penalty = min(0.5, (age_days - 90) / 360)
            except Exception:
                logging.getLogger(__name__).warning(f"Caught unexpected error", exc_info=True)
                pass
        score = max(0.0, 1.0 - time_sensitive_count * self.time_sensitive_penalty - age_penalty)

        if score >= 0.5:
            return score, f"时效性可接受（时间敏感词: {time_sensitive_count}个）"
        else:
            return score, f"时效性不足（时间敏感词: {time_sensitive_count}个, 年龄扣分: {age_penalty:.2f}）"

    def _check_obsolete_signals(self, premise: str,
                                current_context: str) -> Tuple[float, str]:
        """过时信号检查"""
        premise_lower = premise.lower()

        # 1. 前提中是否包含过时信号词
        obsolete_in_premise = sum(1 for kw in self.OBSOLETE_SIGNALS
                                  if kw.lower() in premise_lower)

        if obsolete_in_premise > 0:
            score = max(0.0, 1.0 - obsolete_in_premise * 0.3)
            return score, f"前提包含过时信号词（{obsolete_in_premise}个）"

        # 2. 当前上下文中是否明确提到替代方案（暗示前提过时）
        context_lower = current_context.lower()
        replacement_signals = ["替代", "取代", "新版", "新方案",
                               "replace", "alternative", "new version", "superseded"]
        replacement_count = sum(1 for s in replacement_signals if s in context_lower)

        if replacement_count > 0:
            # 检查替代方案是否针对前提中的核心概念
            core_concepts = self._extract_core_concepts(premise)
            for concept in core_concepts:
                if concept in context_lower:
                    return 0.4, f"上下文中提到 '{concept}' 的替代方案"

        return 1.0, "无过时信号"

    @staticmethod
    def _extract_words(text: str) -> List[str]:
        """提取有意义的词（中文2字以上 + 英文3字母以上）"""
        zh_words = re.findall(r'[\u4e00-\u9fa5]{2,}', text)
        en_words = re.findall(r'[a-zA-Z]{3,}', text)
        return [w.lower() for w in zh_words + en_words]

    @staticmethod
    def _extract_core_concepts(text: str) -> List[str]:
        """提取核心概念词（名词性短语）"""
        # 中文：XX技术/方法/框架/系统
        concepts = re.findall(r'[\u4e00-\u9fa5]{2,8}(?:技术|方法|框架|系统|工具|平台|语言|库|引擎|模型|算法|协议|模式|架构)', text)
        # 英文：技术术语（大写缩写、CamelCase）
        concepts += re.findall(r'\b[A-Z]{2,10}\b', text)
        concepts += re.findall(r'\b[A-Z][a-z]+[A-Z]\w+\b', text)
        return [c.lower() for c in concepts]
