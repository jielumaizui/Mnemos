# -*- coding: utf-8 -*-
"""Security tagging for user-supplied ingestion content."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Iterable, List


@dataclass(frozen=True)
class IngestionSecurityAssessment:
    detected: bool
    score: float
    risk: str
    reason: str
    categories: List[str]
    patterns: List[str]

    @property
    def blocked(self) -> bool:
        # Raw is lossless and local.  Suspicious text is tagged and contained
        # by source authority at derived-write boundaries, never deleted here.
        return False

    @property
    def decision(self) -> str:
        if self.detected:
            return "tagged_prompt_injection"
        return "clean"

    @property
    def tags(self) -> List[str]:
        tags = [
            "x-security=checked",
            f"x-security-score={self.score:.2f}",
            f"x-risk={self.risk}",
        ]
        if self.detected:
            tags.append("x-security=prompt-injection")
        if self.categories:
            tags.append(f"x-threat={','.join(self.categories)}")
        return tags

    def as_dict(self) -> Dict[str, Any]:
        return {
            "security_tags": self.tags,
            "security_score": self.score,
            "security_risk": self.risk,
            "security_decision": self.decision,
            "security_categories": self.categories,
            "security_reason": self.reason,
            "security_containment": (
                "source_authority" if self.detected else "not_required"
            ),
        }


def assess_ingestion_security(content: str) -> IngestionSecurityAssessment:
    """Assess prompt-injection risk for user-supplied ingestion content."""
    from core.kia.ingest_helpers import detect_prompt_injection

    detected, score, reason, patterns, detail = detect_prompt_injection(content or "")
    risk = "high" if score >= 0.85 else "medium" if detected else "low"
    categories = [str(item) for item in detail.get("categories", [])]
    return IngestionSecurityAssessment(
        detected=bool(detected),
        score=round(float(score), 4),
        risk=risk,
        reason=str(reason),
        categories=categories,
        patterns=[str(item) for item in patterns],
    )


def merge_security_tags(tags: Iterable[str], assessment: IngestionSecurityAssessment) -> List[str]:
    """Append security tags without duplicating caller-provided tags."""
    merged = list(tags)
    for tag in assessment.tags:
        if tag not in merged:
            merged.append(tag)
    return merged


def attach_security_fields(
    payload: Dict[str, Any],
    assessment: IngestionSecurityAssessment,
) -> Dict[str, Any]:
    """Add common security metadata to an ingestion result payload."""
    payload.update(assessment.as_dict())
    return payload
