# -*- coding: utf-8 -*-
"""Noise filtering and rule/V2 prejudgment stages for distillation."""

from __future__ import annotations

import logging
import re
from typing import Any, Dict, List, Tuple

from core.config import get_config
from core.hephaestus.distillation_text import build_session_text
from core.kia.ingest_helpers import is_noise_message

logger = logging.getLogger(__name__)

VALUE_PREJUDGMENT_RULE_ASSESSMENT_LENGTH = 3000


def _try_init(
    module_path: str,
    class_name: str,
    log_level: str = "warning",
    log_msg: str = "",
    *args,
    **kwargs,
) -> Any:
    """懒加载并实例化类，失败时返回 None 并记录日志。"""
    from core.import_guard import assert_allowed_module

    try:
        assert_allowed_module(module_path)
        module = __import__(module_path, fromlist=[class_name])
        cls = getattr(module, class_name)
        return cls(*args, **kwargs)
    except (OSError, ValueError, TypeError, KeyError, ImportError, AttributeError, RuntimeError):
        if log_msg:
            getattr(logger, log_level, logger.warning)(log_msg, exc_info=True)
        return None


class NoiseFilter:
    """第1层：噪音过滤 — 规则级，<1ms"""

    def filter(self, messages: List[Dict]) -> Tuple[List[Dict], Dict]:
        filtered = []
        noise_count = 0
        for msg in messages:
            content = msg.get("content", "")
            role = msg.get("role", "")
            if role == "system":
                filtered.append(msg)
                continue
            if is_noise_message(content):
                noise_count += 1
                continue
            filtered.append(msg)

        stats = {"total": len(messages), "noise": noise_count, "kept": len(filtered)}
        return filtered, stats


class ValuePrejudgment:
    """第2层：价值预判 — 规则 + 贝叶斯"""

    CERTAINLY_YES = "CERTAINLY_YES"
    CERTAINLY_NO = "CERTAINLY_NO"
    MAYBE = "MAYBE"

    _KNOWLEDGE_SIGNALS = [
        "原来",
        "本质",
        "根因",
        "因为",
        "所以",
        "导致",
        "解决",
        "修复",
        "选",
        "决定",
        "采用",
        "而非",
        "避免",
        "不要",
        "切忌",
        "步骤",
        "方法",
        "原则",
        "经验",
        "教训",
        "踩坑",
        "偏好",
        "用户要求",
        "用户希望",
        "复盘",
        "纠正",
        "关系更新",
        "触发场景",
        "下次",
        "最佳实践",
        "because",
        "therefore",
        "solution",
        "decided",
        "avoid",
        "best practice",
        "root cause",
        "lesson",
        "pitfall",
        "preference",
        "retrospective",
        "correction",
        "relationship update",
        "next time",
    ]

    _NOISE_SIGNALS = [
        "好的",
        "收到",
        "谢谢",
        "嗯",
        "哦",
        "了解",
        "ok",
        "thanks",
        "got it",
        "sure",
        "fine",
    ]

    def __init__(self):
        self._distill_scorer_v2 = None

    def _get_scorer_v2(self):
        """获取 V2 评分器（懒加载，失败静默回退）。"""
        if self._distill_scorer_v2 is None:
            self._distill_scorer_v2 = _try_init(
                "core.scoring.scorers.distill_scorer_v2",
                "DistillScorerV2",
                log_level="debug",
                log_msg="DistillScorerV2 not available, falling back to rule",
            )
        return self._distill_scorer_v2

    @staticmethod
    def _is_noise_session(messages: List[Dict]) -> bool:
        """当所有非空消息内容均为噪声时判定为噪声会话。"""
        non_empty = [m.get("content", "") for m in messages if m.get("content")]
        if not non_empty:
            return True
        return all(is_noise_message(content) for content in non_empty)

    def judge(self, messages: List[Dict]) -> Tuple[str, float]:
        """预判会话价值，返回 (结论, 置信度)。"""
        session_text = build_session_text(messages)
        if not session_text:
            return self.CERTAINLY_NO, 0.9

        if self._is_noise_session(messages):
            return self.CERTAINLY_NO, 0.95

        rule_score = self._rule_assessment(session_text)
        v2_score = None
        scorer_v2 = self._get_scorer_v2()
        if scorer_v2:
            try:
                v2_score = scorer_v2.score(session_text)
            except (OSError, ValueError, TypeError, KeyError, ImportError, AttributeError, RuntimeError):
                logging.getLogger(__name__).debug("V2 scoring failed, falling back to rule")

        if v2_score is not None:
            v2_distill = v2_score.scores.get("distill")
            combined = 0.4 * rule_score + 0.6 * v2_distill if v2_distill is not None else rule_score
        else:
            combined = rule_score

        if combined >= 0.7:
            return self.CERTAINLY_YES, combined
        elif combined <= 0.3:
            return self.CERTAINLY_NO, 1.0 - combined
        return self.MAYBE, combined

    def _rule_assessment(self, text: str) -> float:
        """规则级快速评估"""
        lower = text.lower()
        score = 0.3

        knowledge_hits = sum(1 for sig in self._KNOWLEDGE_SIGNALS if sig in lower)
        score += min(0.4, knowledge_hits * 0.08)

        noise_hits = sum(1 for sig in self._NOISE_SIGNALS if sig in lower)
        score -= min(0.2, noise_hits * 0.05)

        cfg = get_config()
        rule_assessment_length = int(
            cfg.get(
                "distill.value_prejudgment_rule_assessment_length",
                VALUE_PREJUDGMENT_RULE_ASSESSMENT_LENGTH,
            )
            or VALUE_PREJUDGMENT_RULE_ASSESSMENT_LENGTH
        )
        length = len(text)
        if length < 200:
            score -= 0.15
        elif 500 <= length <= rule_assessment_length:
            score += 0.1

        if re.search(r"```|def |class |import |function ", text):
            score += 0.1

        if re.search(r"\?.*\n.*\n", text) or re.search(r"？.*\n.*\n", text):
            score += 0.05

        return max(0.0, min(1.0, score))
