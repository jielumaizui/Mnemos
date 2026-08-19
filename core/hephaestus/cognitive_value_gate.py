"""Cognitive-value admission gate for distillation outputs."""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, Mapping


CONTRIBUTION_PATTERNS: dict[str, tuple[str, ...]] = {
    "decision": (
        "决定",
        "采用",
        "选择",
        "权衡",
        "取舍",
        "而非",
        "而不是",
        "decision",
        "decided",
        "tradeoff",
        "rather than",
    ),
    "method": (
        "方法",
        "步骤",
        "流程",
        "策略",
        "方案",
        "做法",
        "runbook",
        "workflow",
        "how to",
        "playbook",
    ),
    "anti_pattern": (
        "避免",
        "不要",
        "切忌",
        "踩坑",
        "反模式",
        "失败",
        "根因",
        "复现",
        "root cause",
        "pitfall",
        "regression",
    ),
    "preference": (
        "偏好",
        "用户要求",
        "用户希望",
        "习惯",
        "口径",
        "必须",
        "不要",
        "preference",
        "user wants",
    ),
    "relationship_update": (
        "关系",
        "关联",
        "依赖",
        "映射",
        "链路",
        "跨域",
        "影响",
        "connects",
        "relationship",
        "dependency",
    ),
    "evidence": (
        "证据",
        "验证",
        "测试",
        "命令",
        "输出",
        "日志",
        "复现",
        "pytest",
        "trace",
        "evidence",
        "verified",
    ),
    "future_trigger": (
        "下次",
        "以后",
        "触发",
        "适用于",
        "当",
        "如果",
        "复用",
        "next time",
        "when ",
        "if ",
    ),
    "cognitive_consumer": (
        "policy",
        "skill",
        "wiki",
        "search",
        "preflight",
        "guard",
        "mcp",
        "agent",
        "画像",
        "策略补丁",
        "检索",
        "复盘",
        "工作流",
        "规范",
    ),
}

logger = logging.getLogger(__name__)

SOURCE_EVIDENCE_KEYS = {
    "source",
    "来源",
    "source_session",
    "来源会话",
    "source_event_ids",
    "来源事件ID",
    "raw_event_refs",
    "evidence_refs",
    "证据引用",
    "gate_decision_id",
    "门禁决策ID",
}

POSITIVE_LIFECYCLE_KEYS = {
    "search_hits",
    "搜索命中",
    "ref_count",
    "引用数量",
    "source_count",
    "来源数量",
    "reinforcement_count",
    "强化次数",
    "recap_count",
    "review_count",
    "correction_count",
    "user_corrections",
    "referenced_count",
}

NEGATIVE_LIFECYCLE_KEYS = {
    "ignore_count",
    "dismiss_count",
    "ignored_count",
    "no_result_count",
}

COGNITIVE_TEXT_FRONTMATTER_KEYS = {
    "summary",
    "摘要",
    "keywords",
    "关键词",
    "triggers",
    "触发器",
    "decision",
    "决策摘要",
    "distill_intent",
    "蒸馏意图",
    "evidence_refs",
    "证据引用",
    "source",
    "来源",
}


@dataclass(frozen=True)
class CognitiveValueDecision:
    accepted: bool
    disposition: str
    score: float
    threshold: float
    review_margin: float
    reason: str
    contribution_types: tuple[str, ...] = ()
    consumers: tuple[str, ...] = ()
    dimension_scores: Dict[str, float] = field(default_factory=dict)

    def as_dict(self) -> Dict[str, object]:
        return {
            "accepted": self.accepted,
            "disposition": self.disposition,
            "score": round(self.score, 3),
            "threshold": round(self.threshold, 3),
            "review_margin": round(self.review_margin, 3),
            "reason": self.reason,
            "contribution_types": list(self.contribution_types),
            "consumers": list(self.consumers),
            "dimension_scores": {
                key: round(value, 3) for key, value in self.dimension_scores.items()
            },
        }


class CognitiveValueGate:
    """Deterministic gate for "does this improve the cognitive system?"."""

    def __init__(self, base_threshold: float = 0.55, review_margin: float = 0.15):
        self.base_threshold = base_threshold
        self.review_margin = review_margin

    def evaluate(
        self,
        content: str,
        *,
        frontmatter: Mapping[str, Any] | None = None,
        lifecycle_signals: Mapping[str, Any] | None = None,
    ) -> CognitiveValueDecision:
        fm = dict(frontmatter or {})
        signals = {**self._lifecycle_signals_from_frontmatter(fm), **dict(lifecycle_signals or {})}
        text = self._combined_text(content, fm)
        contribution_types = self._detect_contribution_types(text)
        consumers = self._detect_consumers(text, contribution_types)
        dimension_scores = self.score_content(
            text,
            fm,
            signals,
            contribution_types=contribution_types,
            consumers=consumers,
        )
        score = self._weighted_score(dimension_scores)

        if not contribution_types:
            self._record_profile_usage(
                action_changed=False,
                outcome="missing_cognitive_contribution",
            )
            return CognitiveValueDecision(
                accepted=False,
                disposition="reject",
                score=score,
                threshold=self.base_threshold,
                review_margin=self.review_margin,
                reason="missing_cognitive_contribution",
                contribution_types=(),
                consumers=consumers,
                dimension_scores=dimension_scores,
            )

        if score >= self.base_threshold:
            self._record_profile_usage(
                action_changed=True,
                outcome="cognitive_contribution_meets_threshold",
            )
            return CognitiveValueDecision(
                accepted=True,
                disposition="accept",
                score=score,
                threshold=self.base_threshold,
                review_margin=self.review_margin,
                reason="cognitive_contribution_meets_threshold",
                contribution_types=contribution_types,
                consumers=consumers,
                dimension_scores=dimension_scores,
            )

        if score >= self.base_threshold - self.review_margin:
            self._record_profile_usage(
                action_changed=True,
                outcome="cognitive_contribution_needs_review",
            )
            return CognitiveValueDecision(
                accepted=False,
                disposition="review",
                score=score,
                threshold=self.base_threshold,
                review_margin=self.review_margin,
                reason="cognitive_contribution_needs_review",
                contribution_types=contribution_types,
                consumers=consumers,
                dimension_scores=dimension_scores,
            )

        self._record_profile_usage(
            action_changed=False,
            outcome="cognitive_contribution_below_threshold",
        )
        return CognitiveValueDecision(
            accepted=False,
            disposition="reject",
            score=score,
            threshold=self.base_threshold,
            review_margin=self.review_margin,
            reason="cognitive_contribution_below_threshold",
            contribution_types=contribution_types,
            consumers=consumers,
            dimension_scores=dimension_scores,
        )

    @staticmethod
    def _record_profile_usage(*, action_changed: bool, outcome: str) -> None:
        """Do not read profile claims without a server-resolved principal.

        This background quality gate has no authenticated session scope.  It
        therefore cannot inspect profile assertions merely to emit a usage
        metric; a future authorized caller must pass an explicit profile-use
        receipt instead.
        """

        logger.debug(
            "quality gate profile usage skipped: principal and scope are required"
        )

    @staticmethod
    def score_content(
        text: str,
        frontmatter: Mapping[str, Any],
        lifecycle_signals: Mapping[str, Any],
        *,
        contribution_types: Iterable[str],
        consumers: Iterable[str],
    ) -> Dict[str, float]:
        types = tuple(contribution_types)
        consumer_values = tuple(consumers)
        return {
            "source_evidence": CognitiveValueGate._source_evidence_score(text, frontmatter),
            "contribution_type": min(1.0, len(types) / 4),
            "future_trigger": 1.0 if "future_trigger" in types else _regex_score(text, r"下次|以后|当.+时|如果|when |if "),
            "consumer_impact": min(1.0, len(consumer_values) / 3),
            "lifecycle_signal": CognitiveValueGate._lifecycle_score(lifecycle_signals),
        }

    @staticmethod
    def _weighted_score(scores: Mapping[str, float]) -> float:
        weights = {
            "source_evidence": 0.20,
            "contribution_type": 0.35,
            "future_trigger": 0.15,
            "consumer_impact": 0.20,
            "lifecycle_signal": 0.10,
        }
        return sum(float(scores.get(key, 0.0)) * weight for key, weight in weights.items())

    @staticmethod
    def _combined_text(content: str, frontmatter: Mapping[str, Any]) -> str:
        fm_values = " ".join(
            str(frontmatter.get(key))
            for key in COGNITIVE_TEXT_FRONTMATTER_KEYS
            if frontmatter.get(key)
        )
        return f"{content or ''}\n{fm_values}".lower()

    @staticmethod
    def _detect_contribution_types(text: str) -> tuple[str, ...]:
        detected = []
        for contribution_type, patterns in CONTRIBUTION_PATTERNS.items():
            if contribution_type == "cognitive_consumer":
                continue
            if any(pattern.lower() in text for pattern in patterns):
                detected.append(contribution_type)
        return tuple(detected)

    @staticmethod
    def _detect_consumers(text: str, contribution_types: Iterable[str]) -> tuple[str, ...]:
        consumers = set()
        if any(pattern.lower() in text for pattern in CONTRIBUTION_PATTERNS["cognitive_consumer"]):
            consumers.add("cognitive_runtime")
        types = set(contribution_types)
        if types & {"decision", "method", "anti_pattern", "future_trigger"}:
            consumers.add("preflight_guard")
        if types & {"preference", "relationship_update"}:
            consumers.add("persona_or_graph")
        if types & {"evidence", "method"}:
            consumers.add("wiki_search")
        return tuple(sorted(consumers))

    @staticmethod
    def _source_evidence_score(text: str, frontmatter: Mapping[str, Any]) -> float:
        if any(_has_value(frontmatter.get(key)) for key in SOURCE_EVIDENCE_KEYS):
            return 1.0
        if re.search(r"证据|验证|测试|日志|输出|复现|pytest|trace|evidence|verified", text):
            return 0.7
        if "```" in text:
            return 0.4
        return 0.0

    @staticmethod
    def _lifecycle_score(lifecycle_signals: Mapping[str, Any]) -> float:
        positive = sum(_numeric(lifecycle_signals.get(key)) for key in POSITIVE_LIFECYCLE_KEYS)
        negative = sum(_numeric(lifecycle_signals.get(key)) for key in NEGATIVE_LIFECYCLE_KEYS)
        if positive <= 0 and negative <= 0:
            return 0.0
        return max(0.0, min(1.0, positive / 3 - negative / 5))

    @staticmethod
    def _lifecycle_signals_from_frontmatter(frontmatter: Mapping[str, Any]) -> Dict[str, Any]:
        keys = POSITIVE_LIFECYCLE_KEYS | NEGATIVE_LIFECYCLE_KEYS
        return {key: frontmatter[key] for key in keys if key in frontmatter}


def _regex_score(text: str, pattern: str) -> float:
    return 1.0 if re.search(pattern, text, flags=re.IGNORECASE) else 0.0


def _numeric(value: Any) -> float:
    if isinstance(value, bool):
        return 1.0 if value else 0.0
    try:
        return max(0.0, float(value or 0))
    except (TypeError, ValueError):
        return 1.0 if value else 0.0


def _has_value(value: Any) -> bool:
    if value is None:
        return False
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, (list, tuple, set, dict)):
        return bool(value)
    return True
