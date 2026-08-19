# -*- coding: utf-8 -*-
"""Feedback-loop stage for distillation results."""

from __future__ import annotations

import logging
from typing import Dict, List

from core.hephaestus.distillation_models import DistillationResult
from core.hephaestus.distillation_prejudge import ValuePrejudgment

logger = logging.getLogger(__name__)


class DistillFeedbackLoop:
    """第7层：反馈循环 — 评分驱动"""

    @staticmethod
    def _detect_prejudgment_mismatch(result: DistillationResult) -> List[Dict]:
        """L2 预判与 L3 判断不一致时生成反馈信号。"""
        signals = []
        if result.prejudgment == ValuePrejudgment.CERTAINLY_NO and result.judgment == "knowledge":
            signals.append(
                {
                    "type": "prejudgment_mismatch",
                    "dimension": "distill_score",
                    "expected": 0.3,
                    "actual": 0.7,
                    "reason": "预判为低价值但LLM判断为知识，应调高预判阈值",
                }
            )
        if result.prejudgment == ValuePrejudgment.CERTAINLY_YES and result.judgment == "skip":
            signals.append(
                {
                    "type": "prejudgment_mismatch",
                    "dimension": "distill_score",
                    "expected": 0.7,
                    "actual": 0.3,
                    "reason": "预判为高价值但LLM判断为跳过，应调低预判阈值",
                }
            )
        return signals

    @staticmethod
    def _detect_self_check_quality(result: DistillationResult) -> List[Dict]:
        """根据自检通过率生成质量反馈信号。"""
        if not result.fragments:
            return []
        failed_count = sum(1 for f in result.fragments if not f.self_check_passed)
        fail_rate = failed_count / len(result.fragments)
        if fail_rate > 0.5:
            return [
                {
                    "type": "self_check_failure",
                    "dimension": "quality_score",
                    "expected": 0.7,
                    "actual": 1.0 - fail_rate,
                    "reason": f"自检失败率 {fail_rate:.0%}，提取质量需改善",
                }
            ]
        if fail_rate == 0.0 and len(result.fragments) >= 3:
            return [
                {
                    "type": "high_quality_extraction",
                    "dimension": "quality_score",
                    "expected": 0.9,
                    "actual": 1.0,
                    "reason": f"高质量提取：{len(result.fragments)} 个片段，自检全部通过",
                }
            ]
        return []

    @staticmethod
    def _detect_zero_extraction(result: DistillationResult) -> List[Dict]:
        """判断为知识但无片段时生成信号。"""
        if result.judgment == "knowledge" and not result.fragments:
            return [
                {
                    "type": "zero_extraction",
                    "dimension": "distill_score",
                    "expected": 0.6,
                    "actual": 0.2,
                    "reason": "判断为知识但提取无片段，提取逻辑需改善",
                }
            ]
        return []

    @staticmethod
    def _detect_link_strength(result: DistillationResult) -> List[Dict]:
        """根据跨 Agent 链接数量生成强弱信号。"""
        if result.judgment != "knowledge":
            return []
        total_links = len(result.cross_agent_links)
        if total_links >= 3:
            return [
                {
                    "type": "cross_agent_link_strong",
                    "dimension": "link_score",
                    "expected": 0.8,
                    "actual": 1.0,
                    "reason": f"强跨Agent关联：{total_links} 条链接",
                }
            ]
        if total_links == 0 and result.fragments:
            return [
                {
                    "type": "cross_agent_link_weak",
                    "dimension": "link_score",
                    "expected": 0.6,
                    "actual": 0.2,
                    "reason": "知识未关联到任何已有页面，关联逻辑需改善",
                }
            ]
        return []

    @staticmethod
    def _detect_fragment_diversity(result: DistillationResult) -> List[Dict]:
        """根据片段形态多样性生成信号。"""
        if not result.fragments or len(result.fragments) < 2:
            return []
        forms = [f.form for f in result.fragments]
        unique_forms = len(set(forms))
        if unique_forms == 1:
            return [
                {
                    "type": "fragment_diversity_low",
                    "dimension": "diversity_score",
                    "expected": 0.7,
                    "actual": 0.3,
                    "reason": f"所有片段均为同一形态 '{forms[0]}'，形态多样性不足",
                }
            ]
        if unique_forms >= 3:
            return [
                {
                    "type": "fragment_diversity_high",
                    "dimension": "diversity_score",
                    "expected": 0.8,
                    "actual": 1.0,
                    "reason": f"高形态多样性：{unique_forms} 种形态",
                }
            ]
        return []

    @staticmethod
    def _detect_yield(result: DistillationResult) -> List[Dict]:
        """根据片段数量生成丰度/稀疏信号。"""
        if result.judgment != "knowledge" or not result.fragments:
            return []
        count = len(result.fragments)
        if count >= 5:
            return [
                {
                    "type": "extraction_rich",
                    "dimension": "yield_score",
                    "expected": 0.8,
                    "actual": 1.0,
                    "reason": f"丰富提取：{count} 个知识片段",
                }
            ]
        if count == 1:
            return [
                {
                    "type": "extraction_sparse",
                    "dimension": "yield_score",
                    "expected": 0.6,
                    "actual": 0.3,
                    "reason": "稀疏提取：仅 1 个知识片段",
                }
            ]
        return []

    @staticmethod
    def _log_signals(signals: List[Dict], session_id: str) -> None:
        """汇总并记录反馈信号。"""
        if signals:
            sig_summary = ", ".join(f"{s['type']}({s['dimension']})" for s in signals)
            logger.info(
                "[FeedbackLoop] %s 生成 %s 个反馈信号: %s",
                session_id,
                len(signals),
                sig_summary,
            )
        else:
            logger.debug("[FeedbackLoop] %s 无反馈信号", session_id)

    @staticmethod
    def _compute_fail_rate(fragments: List) -> float:
        """计算片段自检失败率。"""
        if not fragments:
            return 0.0
        failed = sum(1 for f in fragments if not f.self_check_passed)
        return failed / len(fragments)

    def _update_relation_confidence(self, result: DistillationResult) -> None:
        """根据蒸馏结果更新知识关系置信度。"""
        try:
            from core.kia.relation_manager import RelationManager

            rm = RelationManager()
            if result.judgment == "knowledge" and result.fragments:
                feedback = 1.0 if self._compute_fail_rate(result.fragments) < 0.5 else 0.0
            else:
                feedback = 0.0
            updated_count = 0
            for frag in result.fragments or []:
                for rel in frag.relations or []:
                    source = rel.get("source", "")
                    target = (rel.get("target", "") or "").strip("[]")
                    rel_type = rel.get("type", "related_to")
                    if source and target:
                        rm.update_confidence(source, target, rel_type, feedback)
                        updated_count += 1
            if updated_count:
                logger.info(
                    "[FeedbackLoop] 更新 %s 条关系置信度 (feedback=%s)",
                    updated_count,
                    feedback,
                )
        except (
            OSError,
            ValueError,
            TypeError,
            KeyError,
            ImportError,
            AttributeError,
            RuntimeError,
        ):
            logger.debug("[FeedbackLoop] 关系置信度更新失败", exc_info=True)

    @staticmethod
    def _publish_feedback_event(
        result: DistillationResult, signals: List[Dict], session_id: str
    ) -> None:
        """发布 feedback_loop 事件。"""
        try:
            from core.mnemos_bus import publish_event

            publish_event(
                "feedback_loop",
                "distill",
                {
                    "session_id": session_id,
                    "signal_count": len(signals),
                    "signal_types": [s["type"] for s in signals],
                    "judgment": result.judgment,
                    "fragment_count": len(result.fragments) if result.fragments else 0,
                    "cross_agent_links": len(result.cross_agent_links),
                },
            )
        except (
            OSError,
            ValueError,
            TypeError,
            KeyError,
            ImportError,
            AttributeError,
            RuntimeError,
        ):
            logger.warning("[Distillation] feedback_loop 事件发布失败", exc_info=True)

    def _record_adaptive_metrics(self, result: DistillationResult, signals: List[Dict]) -> None:
        """向 AdaptiveConfig 记录蒸馏相关的指标。"""
        try:
            from core.kia.adaptive_config import AdaptiveConfig

            ac = AdaptiveConfig()
            fp = 0.0
            if result.judgment == "knowledge":
                if not result.fragments:
                    fp = 1.0
                elif self._compute_fail_rate(result.fragments) > 0.5:
                    fp = 1.0
            ac.record_usage("distill", "false_positive_rate", fp)
            ac.record_usage("scoring", "feedback_rate", 1.0 if signals else 0.0)
        except (
            OSError,
            ValueError,
            TypeError,
            KeyError,
            ImportError,
            AttributeError,
            RuntimeError,
        ):
            logger.debug("[FeedbackLoop] AdaptiveConfig 指标记录失败", exc_info=True)

    def evaluate(self, result: DistillationResult) -> List[Dict]:
        """评估蒸馏结果，生成反馈信号并驱动模型进化。"""
        session_id = getattr(result, "session_id", "unknown")

        signals: List[Dict] = []
        signals.extend(self._detect_prejudgment_mismatch(result))
        signals.extend(self._detect_self_check_quality(result))
        signals.extend(self._detect_zero_extraction(result))
        signals.extend(self._detect_link_strength(result))
        signals.extend(self._detect_fragment_diversity(result))
        signals.extend(self._detect_yield(result))

        self._log_signals(signals, session_id)
        self._update_relation_confidence(result)
        self._publish_feedback_event(result, signals, session_id)
        self._record_adaptive_metrics(result, signals)

        return signals
