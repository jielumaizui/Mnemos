"""Structured quality gate decisions for distillation and memory ingestion."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Dict, List

from core.system_contracts import QualityDecision, quality_decision_from_gate


@dataclass(frozen=True)
class QualityGateDecision:
    accepted: bool
    disposition: str  # accept | review | reject
    score: float
    threshold: float
    uncertainty: float
    reason: str
    dimension_scores: Dict[str, float] = field(default_factory=dict)

    def as_dict(self) -> Dict[str, object]:
        return {
            "accepted": self.accepted,
            "disposition": self.disposition,
            "score": round(self.score, 3),
            "threshold": round(self.threshold, 3),
            "uncertainty": round(self.uncertainty, 3),
            "reason": self.reason,
            "dimension_scores": {k: round(v, 3) for k, v in self.dimension_scores.items()},
        }

    def as_unified_decision(self, subject: str) -> QualityDecision:
        """Map this local gate result to the system-wide QualityDecision."""
        return quality_decision_from_gate(
            subject=subject,
            disposition=self.disposition,
            reason=self.reason,
            score=self.score,
            evidence_refs=("core/hephaestus/quality_gate.py",),
        )


class QualityGate:
    """Small deterministic gate with explicit uncertainty handling."""

    def __init__(self, base_threshold: float = 0.55, review_margin: float = 0.15):
        self.base_threshold = base_threshold
        self.review_margin = review_margin

    def evaluate(
        self,
        content: str,
        *,
        uncertainty: float = 0.0,
        dimension_scores: Dict[str, float] | None = None,
    ) -> QualityGateDecision:
        scores = dimension_scores or self.score_content(content)
        score = sum(scores.values()) / len(scores) if scores else 0.0
        uncertainty = max(0.0, min(1.0, uncertainty))
        threshold = max(0.25, self.base_threshold - uncertainty * 0.2)

        if score >= threshold:
            return QualityGateDecision(
                accepted=True,
                disposition="accept",
                score=score,
                threshold=threshold,
                uncertainty=uncertainty,
                reason="score_meets_threshold",
                dimension_scores=scores,
            )

        if uncertainty >= 0.6 or score >= threshold - self.review_margin:
            return QualityGateDecision(
                accepted=False,
                disposition="review",
                score=score,
                threshold=threshold,
                uncertainty=uncertainty,
                reason="uncertain_or_near_threshold",
                dimension_scores=scores,
            )

        return QualityGateDecision(
            accepted=False,
            disposition="reject",
            score=score,
            threshold=threshold,
            uncertainty=uncertainty,
            reason="score_below_threshold",
            dimension_scores=scores,
        )

    @staticmethod
    def score_content(content: str) -> Dict[str, float]:
        text = (content or "").strip()
        length_score = min(1.0, len(text) / 600)
        structure_score = 0.2
        if "\n#" in text or text.startswith("#"):
            structure_score += 0.3
        if "```" in text:
            structure_score += 0.25
        if re.search(r"(?m)^[-*] ", text):
            structure_score += 0.15
        structure_score = min(1.0, structure_score)

        noise_penalty = 0.0
        if len(text) < 30:
            noise_penalty += 0.4
        if re.fullmatch(r"[\W_]+", text or ""):
            noise_penalty += 0.5
        if text.lower() in {"ok", "thanks", "好的", "谢谢"}:
            noise_penalty += 0.5
        clarity_score = max(0.0, 1.0 - noise_penalty)

        keyword_score = 0.5
        useful_markers: List[str] = ["decision", "because", "原因", "方案", "验证", "测试", "配置"]
        if any(marker in text.lower() for marker in useful_markers):
            keyword_score = 0.8

        return {
            "length": length_score,
            "structure": structure_score,
            "clarity": clarity_score,
            "usefulness": keyword_score,
        }
