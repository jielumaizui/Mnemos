"""Application-level blindspot, divergence, and freshness detectors."""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List


@dataclass(frozen=True)
class AppSignal:
    kind: str
    topic: str
    confidence: float
    severity: str
    evidence: List[str] = field(default_factory=list)
    suggested_action: str = ""
    cooldown_days: int = 7

    def as_dict(self) -> Dict[str, Any]:
        return {
            "kind": self.kind,
            "topic": self.topic,
            "confidence": round(self.confidence, 3),
            "severity": self.severity,
            "evidence": list(self.evidence),
            "suggested_action": self.suggested_action,
            "cooldown_days": self.cooldown_days,
        }


class AvoidanceSignalDetector:
    """Detect topics that repeatedly appear in searches but get ignored."""

    def __init__(self, min_occurrences: int = 3, significance_threshold: float = 1.0):
        self.min_occurrences = min_occurrences
        self.significance_threshold = significance_threshold

    def detect(self, history: Iterable[Dict[str, Any]]) -> List[AppSignal]:
        entries = list(history)
        if len(entries) < self.min_occurrences:
            return []

        global_rate = self._global_click_rate(entries)
        by_topic: Dict[str, List[Dict[str, Any]]] = {}
        for entry in entries:
            for topic in self._extract_topics(str(entry.get("query", ""))):
                by_topic.setdefault(topic, []).append(entry)

        signals: List[AppSignal] = []
        for topic, topic_entries in by_topic.items():
            if len(topic_entries) < self.min_occurrences:
                continue
            clicked = 0
            for entry in topic_entries:
                clicked_results = " ".join(str(v) for v in entry.get("clicked_results", []))
                if topic.lower() in clicked_results.lower():
                    clicked += 1
            click_rate = clicked / len(topic_entries)
            z_score = self._z_score(click_rate, global_rate, len(topic_entries))
            significance = abs(z_score) if click_rate < global_rate else 0.0
            if significance < self.significance_threshold:
                continue
            confidence = min(1.0, significance / 4.0)
            signals.append(
                AppSignal(
                    kind="avoidance",
                    topic=topic,
                    confidence=confidence,
                    severity="medium" if confidence < 0.75 else "high",
                    evidence=[
                        f"occurrences={len(topic_entries)}",
                        f"topic_click_rate={click_rate:.2f}",
                        f"global_click_rate={global_rate:.2f}",
                        f"z_score={z_score:.2f}",
                    ],
                    suggested_action="在盲区提醒中询问用户是否要补充该主题知识",
                    cooldown_days=14,
                )
            )
        return sorted(signals, key=lambda item: item.confidence, reverse=True)

    @staticmethod
    def _global_click_rate(entries: List[Dict[str, Any]]) -> float:
        shown = sum(len(entry.get("results_shown", []) or []) for entry in entries)
        clicked = sum(len(entry.get("clicked_results", []) or []) for entry in entries)
        return clicked / shown if shown else 0.3

    @staticmethod
    def _z_score(rate: float, expected: float, n: int) -> float:
        if n <= 0 or expected <= 0 or expected >= 1:
            return 0.0
        std_error = math.sqrt(expected * (1 - expected) / n)
        return (rate - expected) / std_error if std_error else 0.0

    @staticmethod
    def _extract_topics(text: str) -> List[str]:
        zh = re.findall(r"[\u4e00-\u9fa5]{2,8}(?:技术|方法|框架|系统|工具|问题|方案|优化|设计|架构|模式)", text)
        en = re.findall(r"\b[A-Za-z][A-Za-z0-9_-]{3,}\b", text)
        stopwords = {"what", "when", "where", "which", "with", "from", "that", "this"}
        return [t.lower() for t in zh + en if t.lower() not in stopwords]


class CrossAgentDivergenceDetector:
    """Detect divergent claims about the same topic across agents."""

    def __init__(self, min_score: float = 0.3):
        self.min_score = min_score

    def detect(self, outputs: Iterable[Dict[str, Any]]) -> List[AppSignal]:
        by_topic: Dict[str, List[Dict[str, Any]]] = {}
        for output in outputs:
            topic = str(output.get("topic") or "general")
            by_topic.setdefault(topic, []).append(output)

        signals: List[AppSignal] = []
        for topic, topic_outputs in by_topic.items():
            if len(topic_outputs) < 2:
                continue
            texts = [str(item.get("output", "")) for item in topic_outputs]
            confidences = [float(item.get("confidence", 0.5) or 0.5) for item in topic_outputs]
            avg_similarity = self._average_similarity(texts)
            confidence_std = self._stddev(confidences)
            score = (1.0 - avg_similarity) * 0.65 + min(1.0, confidence_std * 3) * 0.35
            if score < self.min_score:
                continue
            signals.append(
                AppSignal(
                    kind="cross_agent_divergence",
                    topic=topic,
                    confidence=min(1.0, score),
                    severity="high" if score >= 0.7 else "medium",
                    evidence=[
                        "agents="
                        + ",".join(
                            str(item.get("agent_id") or item.get("agent") or "unknown")
                            for item in topic_outputs
                        ),
                        f"avg_similarity={avg_similarity:.2f}",
                        f"confidence_std={confidence_std:.2f}",
                    ],
                    suggested_action="交给 dispute_resolver 或知识图谱关系复核",
                    cooldown_days=7,
                )
            )
        return sorted(signals, key=lambda item: item.confidence, reverse=True)

    @classmethod
    def _average_similarity(cls, texts: List[str]) -> float:
        scores: List[float] = []
        for i, left in enumerate(texts):
            for right in texts[i + 1 :]:
                scores.append(cls._similarity(left, right))
        return sum(scores) / len(scores) if scores else 1.0

    @staticmethod
    def _similarity(left: str, right: str) -> float:
        left_words = set(re.findall(r"[\u4e00-\u9fa5]{2,}|[a-zA-Z]{3,}", left.lower()))
        right_words = set(re.findall(r"[\u4e00-\u9fa5]{2,}|[a-zA-Z]{3,}", right.lower()))
        if not left_words or not right_words:
            return 0.0
        return len(left_words & right_words) / len(left_words | right_words)

    @staticmethod
    def _stddev(values: List[float]) -> float:
        if len(values) < 2:
            return 0.0
        mean = sum(values) / len(values)
        return math.sqrt(sum((value - mean) ** 2 for value in values) / len(values))


class FreshnessSignalChecker:
    """Small freshness checker for application-layer diagnostics."""

    def __init__(self, half_life_days: int = 30, stale_threshold: float = 0.25):
        self.half_life_days = half_life_days
        self.stale_threshold = stale_threshold

    def check(self, page: Dict[str, Any]) -> AppSignal | None:
        last_modified = self._parse_datetime(page.get("last_modified") or page.get("updated_at"))
        if last_modified is None:
            return AppSignal(
                kind="freshness",
                topic=str(page.get("title") or page.get("path") or "unknown"),
                confidence=0.8,
                severity="medium",
                evidence=["missing_last_modified"],
                suggested_action="补齐页面 frontmatter 的更新时间或进入待复核",
                cooldown_days=30,
            )

        age_days = max(0, (_utc_now() - last_modified).days)
        freshness = math.exp(-age_days / max(1, self.half_life_days))
        if freshness >= self.stale_threshold:
            return None
        return AppSignal(
            kind="freshness",
            topic=str(page.get("title") or page.get("path") or "unknown"),
            confidence=min(1.0, 1.0 - freshness),
            severity="high" if freshness < self.stale_threshold / 2 else "medium",
            evidence=[f"age_days={age_days}", f"freshness_score={freshness:.3f}"],
            suggested_action="交给 freshness_refresh_worker 或提醒用户确认是否更新",
            cooldown_days=30,
        )

    @staticmethod
    def _parse_datetime(value: Any) -> datetime | None:
        if not value:
            return None
        try:
            parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            return parsed.astimezone(timezone.utc)
        except ValueError:
            return None


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)
