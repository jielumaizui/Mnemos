"""V2-compatible domain scorers for public scoring dimensions."""

from __future__ import annotations

import re
from typing import Any, Dict, Type

from core.scoring.adaptive_scorer_v2 import ScoreCardV2


SCORER_DIMENSIONS: Dict[str, tuple[str, ...]] = {
    "sync": ("noise_score", "urgency_score", "sync_priority"),
    "raw_memory": ("quality_score", "sensitivity_score", "fragmentation_score"),
    "kg": ("entity_quality", "relation_confidence", "knowledge_freshness"),
    "profile": ("behavior_pattern", "blind_spot_score", "preference_stability"),
    "ops": ("anomaly_score", "health_score", "capacity_risk"),
}


def _clamp(value: float) -> float:
    return max(0.0, min(1.0, value))


def _features(content: str, metadata: Dict[str, Any] | None = None) -> Dict[str, Any]:
    metadata = metadata or {}
    text = content or ""
    return {
        "content": text,
        "metadata": metadata,
        "length": len(text),
        "has_code": "```" in text,
        "has_list": bool(re.search(r"(?m)^[-*] ", text)),
        "question_marks": text.count("?") + text.count("？"),
        "lower": text.lower(),
    }


class BaseDomainScorer:
    domain = ""
    dimensions: tuple[str, ...] = ()

    def score(self, content: str, metadata: Dict[str, Any] | None = None) -> ScoreCardV2:
        features = _features(content, metadata)
        scores = self.score_features(features)
        return ScoreCardV2(
            scores={name: _clamp(scores.get(name, 0.0)) for name in self.dimensions},
            confidences={name: 0.55 for name in self.dimensions},
            features=features,
            model_version=f"{self.domain}-rules-v2",
        )

    def score_features(self, features: Dict[str, Any]) -> Dict[str, float]:
        raise NotImplementedError


class SyncDomainScorer(BaseDomainScorer):
    domain = "sync"
    dimensions = SCORER_DIMENSIONS["sync"]

    def score_features(self, features: Dict[str, Any]) -> Dict[str, float]:
        lower = features["lower"]
        length = features["length"]
        noise = 0.8 if lower.strip() in {"ok", "thanks", "好的", "谢谢"} or length < 20 else 0.1
        urgent_hits = sum(
            1
            for word in ("error", "crash", "fatal", "outage", "异常", "崩溃", "紧急", "线上")
            if word in lower
        )
        urgency = _clamp(0.1 + urgent_hits * 0.25)
        priority = 0.25 + (0.3 if features["has_code"] else 0.0) + min(0.3, length / 1200)
        return {
            "noise_score": noise,
            "urgency_score": urgency,
            "sync_priority": priority,
        }


class RawMemoryScorer(BaseDomainScorer):
    domain = "raw_memory"
    dimensions = SCORER_DIMENSIONS["raw_memory"]
    _SENSITIVE = re.compile(
        r"sk-[A-Za-z0-9_-]{12,}|"
        r"gh[pousr]_[A-Za-z0-9_]{20,}|"
        r"password[:=]\s*\S+|"
        r"token[:=]\s*\S+|"
        r"secret[:=]\s*\S+",
        re.I,
    )

    def score_features(self, features: Dict[str, Any]) -> Dict[str, float]:
        text = features["content"]
        length = features["length"]
        quality = _clamp(min(0.75, length / 800) + (0.2 if features["has_list"] else 0.0))
        sensitivity = 1.0 if self._SENSITIVE.search(text) else 0.0
        fragmentation = 0.2
        if length < 120:
            fragmentation += 0.4
        if "segment=" in text or "type=chunk" in text:
            fragmentation += 0.3
        return {
            "quality_score": quality,
            "sensitivity_score": sensitivity,
            "fragmentation_score": _clamp(fragmentation),
        }


class KGDomainScorer(BaseDomainScorer):
    domain = "kg"
    dimensions = SCORER_DIMENSIONS["kg"]

    def score_features(self, features: Dict[str, Any]) -> Dict[str, float]:
        text = features["content"]
        lower = features["lower"]
        entity_pattern = (
            r"\[\[^\]]+\]\]|"
            r"\b[A-Z][A-Za-z0-9_]{2,}\b|"
            r"[\u4e00-\u9fa5]{2,8}(?:系统|框架|工具|模型)"
        )
        entities = re.findall(entity_pattern, text)
        relation_words = ("depends", "uses", "related", "依赖", "基于", "关联", "使用")
        relation_hits = sum(1 for word in relation_words if word in lower)
        metadata = features.get("metadata", {})
        age_days = float(metadata.get("age_days", 0) or 0)
        freshness = _clamp(1.0 - age_days / 365)
        return {
            "entity_quality": _clamp(len(entities) / 5),
            "relation_confidence": _clamp(0.25 + relation_hits * 0.2 + (0.2 if "[[" in text else 0.0)),
            "knowledge_freshness": freshness,
        }


class ProfileDomainScorer(BaseDomainScorer):
    domain = "profile"
    dimensions = SCORER_DIMENSIONS["profile"]

    def score_features(self, features: Dict[str, Any]) -> Dict[str, float]:
        lower = features["lower"]
        behavior = 0.25 + (0.2 if features["has_code"] else 0.0) + (0.15 if "always" in lower or "总是" in lower else 0.0)
        blindspot = _clamp(0.2 + min(0.4, features["question_marks"] * 0.12))
        if any(word in lower for word in ("不了解", "不清楚", "第一次", "unknown", "why", "how")):
            blindspot += 0.2
        stability = 0.65 if any(word in lower for word in ("prefer", "偏好", "习惯", "always", "总是")) else 0.45
        return {
            "behavior_pattern": _clamp(behavior),
            "blind_spot_score": _clamp(blindspot),
            "preference_stability": _clamp(stability),
        }


class OpsDomainScorer(BaseDomainScorer):
    domain = "ops"
    dimensions = SCORER_DIMENSIONS["ops"]

    def score_features(self, features: Dict[str, Any]) -> Dict[str, float]:
        lower = features["lower"]
        errors = sum(1 for word in ("error", "fail", "timeout", "crash", "异常", "失败", "超时") if word in lower)
        success = sum(1 for word in ("ok", "healthy", "success", "成功", "正常") if word in lower)
        capacity = sum(1 for word in ("queue", "disk", "memory", "容量", "积压", "队列") if word in lower)
        anomaly = _clamp(0.1 + errors * 0.25)
        return {
            "anomaly_score": anomaly,
            "health_score": _clamp(0.75 + success * 0.08 - errors * 0.18),
            "capacity_risk": _clamp(0.1 + capacity * 0.2),
        }


DOMAIN_SCORERS: Dict[str, Type[BaseDomainScorer]] = {
    "sync": SyncDomainScorer,
    "raw_memory": RawMemoryScorer,
    "kg": KGDomainScorer,
    "profile": ProfileDomainScorer,
    "ops": OpsDomainScorer,
}


def score_domain(
    domain: str,
    content: str,
    metadata: Dict[str, Any] | None = None,
) -> ScoreCardV2:
    scorer_cls = DOMAIN_SCORERS[domain]
    return scorer_cls().score(content, metadata=metadata)


def dimension_catalog() -> Dict[str, tuple[str, ...]]:
    return dict(SCORER_DIMENSIONS)
