# -*- coding: utf-8 -*-
"""Behavior/intent signal helpers for distillation."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Mapping, Sequence, cast

from core.cognitive.sources import ContentSource, UserIntent


INTENT_STATUS_VALUES = {"verified", "refuted", "revised", "unverified", "unknown"}


@dataclass(frozen=True)
class BehaviorIntentSignal:
    """System-side behavior/intent hint passed into the extractor prompt."""

    content_source: str = ContentSource.UNKNOWN.value
    user_intent_signal: str = UserIntent.UNKNOWN.value
    intent_hypothesis: str = "unknown"
    intent_status: str = "unverified"
    intent_confidence: float = 0.3
    behavior_summary: str = "用户引入这条材料的原因尚未被验证。"
    intent_evidence: list[dict[str, str]] = field(default_factory=list)
    intent_verification_events: list[dict[str, str]] = field(default_factory=list)
    router_intent: str = "unknown"
    router_confidence: float = 0.0
    router_needs_correction: bool = False

    def as_prompt_dict(self) -> dict[str, Any]:
        return {
            "content_source": self.content_source,
            "user_intent_signal": self.user_intent_signal,
            "intent_hypothesis": self.intent_hypothesis,
            "intent_status": self.intent_status,
            "intent_confidence": round(self.intent_confidence, 2),
            "behavior_summary": self.behavior_summary,
            "intent_evidence": self.intent_evidence,
            "intent_verification_events": self.intent_verification_events,
            "intent_router": {
                "intent": self.router_intent,
                "confidence": round(self.router_confidence, 2),
                "needs_correction": self.router_needs_correction,
            },
        }


class _NoopCorrectionStore:
    def lookup(self, _user_input: str) -> None:
        return None

    def record_correction(
        self, _user_input: str, _original_intent: str, _corrected_intent: str
    ) -> None:
        return None


def infer_behavior_intent_signal(
    messages: Sequence[Mapping[str, Any]] | None,
    *,
    session_id: str = "",
    source_agent: str = "",
) -> BehaviorIntentSignal:
    """Infer a compact behavior/intent hint from messages and existing routers."""
    user_texts = _extract_user_texts(messages or [])
    last_user_text = user_texts[-1] if user_texts else _joined_content(messages or [])
    source = _detect_content_source(messages or [], last_user_text)
    user_intent = _infer_user_intent(last_user_text)
    router_intent, router_confidence, needs_correction = _route_intent(last_user_text)
    status, verification_events = _infer_verification_events(user_texts, session_id)
    if source in {ContentSource.EXTERNAL_FILE, ContentSource.LIKELY_PASTED}:
        # The supplied material can describe an intent, but it is not itself
        # evidence that the user holds that intent.  Keep the pre-signal
        # cautious until a role-local explicit-user message proves otherwise.
        user_intent = UserIntent.UNKNOWN
        router_intent, router_confidence, needs_correction = "unknown", 0.0, False
        status, verification_events = "unverified", []

    hypothesis = _intent_hypothesis(source, user_intent, router_intent)
    confidence = _intent_confidence(source, user_intent, router_confidence, status)
    source_event_id = f"session:{session_id or 'unknown'}"
    evidence = []
    if last_user_text.strip():
        evidence.append(
            {
                "source_event_id": source_event_id,
                "quote": _compact(last_user_text),
                "reason": "latest_user_message",
            }
        )

    return BehaviorIntentSignal(
        content_source=source.value,
        user_intent_signal=user_intent.value,
        intent_hypothesis=hypothesis,
        intent_status=status,
        intent_confidence=confidence,
        behavior_summary=_behavior_summary(source, hypothesis, status, source_agent),
        intent_evidence=evidence,
        intent_verification_events=verification_events,
        router_intent=router_intent,
        router_confidence=router_confidence,
        router_needs_correction=needs_correction,
    )


def format_behavior_intent_context(signal: BehaviorIntentSignal) -> str:
    """Render behavior/intent meta as prompt context."""
    data = signal.as_prompt_dict()
    lines = [
        "## 用户行为/意图输入信号（系统预判，供 LLM 校正）",
        "",
        f"- content_source_hint: {data['content_source']}",
        f"- user_intent_signal: {data['user_intent_signal']}",
        f"- intent_hypothesis_hint: {data['intent_hypothesis']}",
        f"- intent_status_hint: {data['intent_status']}",
        f"- intent_confidence_hint: {data['intent_confidence']}",
        f"- behavior_summary_hint: {data['behavior_summary']}",
        "- intent_router:",
        f"  - intent: {data['intent_router']['intent']}",
        f"  - confidence: {data['intent_router']['confidence']}",
        f"  - needs_correction: {str(data['intent_router']['needs_correction']).lower()}",
        "- intent_evidence_hints:",
    ]
    for item in data["intent_evidence"] or []:
        lines.append(f"  - {item['source_event_id']}: {item['quote']}")
    if not data["intent_evidence"]:
        lines.append("  - none")
    lines.append("- intent_verification_event_hints:")
    for item in data["intent_verification_events"] or []:
        lines.append(
            f"  - {item['source_event_id']}: {item['status']} / {item['quote']}"
        )
    if not data["intent_verification_events"]:
        lines.append("  - none")
    return "\n".join(lines)


def _extract_user_texts(messages: Sequence[Mapping[str, Any]]) -> list[str]:
    texts: list[str] = []
    for msg in messages:
        role = str(msg.get("role", "")).lower()
        content = str(msg.get("content") or "")
        if role == "user" and content.strip():
            texts.append(content.strip())
            continue
        texts.extend(
            m.strip()
            for m in re.findall(r"\[user\]\s*(.*?)(?=\n\[[a-z_]+\]\s|\Z)", content, re.DOTALL)
            if m.strip()
        )
    return texts


def _joined_content(messages: Sequence[Mapping[str, Any]]) -> str:
    return "\n".join(str(msg.get("content") or "") for msg in messages if msg.get("content"))


def _detect_content_source(
    messages: Sequence[Mapping[str, Any]],
    user_text: str,
) -> ContentSource:
    for msg in messages:
        raw_source = msg.get("content_source") or msg.get("source_type")
        if raw_source in {source.value for source in ContentSource}:
            return ContentSource(str(raw_source))
        if msg.get("artifact_path") or msg.get("attachment_path") or msg.get("file_path"):
            return ContentSource.EXTERNAL_FILE

    if len(user_text) > 1200 and not re.search(r"(请|帮我|给我|能否|可以|建议)", user_text):
        return ContentSource.LIKELY_PASTED
    return ContentSource.NATIVE_DIALOGUE if user_text.strip() else ContentSource.UNKNOWN


def _infer_user_intent(user_text: str) -> UserIntent:
    text = user_text.lower()
    if re.search(r"(觉得|认为|评价|判断|分析|怎么看|行不行|可行|靠谱|值得)", text):
        return UserIntent.SEEKING_JUDGMENT
    if re.search(r"(总结|提炼|概括|摘要|精简|整理)", text):
        return UserIntent.SEEKING_SUMMARY
    if re.search(r"(同意|赞同|说得对|有道理|没错|正是|确实)", text):
        return UserIntent.EXPRESSING_AGREEMENT
    if re.search(r"(不对|错了|有问题|质疑|反对|不同意|但是|不过)", text):
        return UserIntent.EXPRESSING_DOUBT
    if re.search(r"(为什么|怎么|如何|什么|哪|吗|呢)[？?]", text):
        return UserIntent.ASKING_QUESTION
    if len(text) > 500 and not re.search(r"(请|帮我|给我|能否|可以|建议)", text):
        return UserIntent.SHARING_INFORMATION
    return UserIntent.UNKNOWN


def _route_intent(user_text: str) -> tuple[str, float, bool]:
    if not user_text.strip():
        return "unknown", 0.0, False
    try:
        from core.app.intent_router import IntentRouter

        decision = IntentRouter(correction_store=cast(Any, _NoopCorrectionStore())).route(
            user_text,
            allow_llm_fallback=False,
        )
        return decision.intent, float(decision.confidence), bool(decision.needs_correction)
    except (
        AttributeError,
        ImportError,
        KeyError,
        OSError,
        RuntimeError,
        TypeError,
        ValueError,
    ):
        return "unknown", 0.0, False


def _infer_verification_events(
    user_texts: Sequence[str],
    session_id: str,
) -> tuple[str, list[dict[str, str]]]:
    if len(user_texts) < 2:
        return "unverified", []
    source_event_id = f"session:{session_id or 'unknown'}"
    events: list[dict[str, str]] = []
    status = "unverified"
    for text in user_texts[1:]:
        compact = _compact(text)
        if re.search(r"(不是|不对|错了|纠正|改成|其实|不是这个意思)", text):
            status = "revised"
            events.append(
                {"source_event_id": source_event_id, "status": status, "quote": compact}
            )
        elif re.search(r"(对|没错|是的|就是|正是|确认|可以这样)", text):
            if status == "unverified":
                status = "verified"
            events.append(
                {"source_event_id": source_event_id, "status": "verified", "quote": compact}
            )
    return status, events


def _intent_hypothesis(
    source: ContentSource,
    user_intent: UserIntent,
    router_intent: str,
) -> str:
    if user_intent != UserIntent.UNKNOWN:
        return str(user_intent.value)
    if router_intent and router_intent not in {"chat", "unknown"}:
        return router_intent
    return "unknown"


def _intent_confidence(
    source: ContentSource,
    user_intent: UserIntent,
    router_confidence: float,
    status: str,
) -> float:
    confidence = max(router_confidence, 0.3)
    if user_intent != UserIntent.UNKNOWN:
        confidence = max(confidence, 0.65)
    if status == "verified":
        confidence = max(confidence, 0.85)
    elif status in {"revised", "refuted"}:
        confidence = min(confidence, 0.65)
    return min(max(confidence, 0.0), 1.0)


def _behavior_summary(
    source: ContentSource,
    hypothesis: str,
    status: str,
    source_agent: str,
) -> str:
    if source == ContentSource.EXTERNAL_FILE:
        return "用户提供了外部文件；具体认可程度和使用意图尚未由显式用户证据确认。"
    if hypothesis != "unknown":
        suffix = "已被后续对话验证。" if status == "verified" else "后续对话尚未验证。"
        agent_part = f" 来源 agent={source_agent}。" if source_agent else ""
        return f"用户通过对话表达了 {hypothesis} 意图，{suffix}{agent_part}"
    return "用户引入材料的具体意图未知，后续对话尚未验证。"


def _compact(text: str, limit: int = 180) -> str:
    compact = re.sub(r"\s+", " ", text).strip()
    if len(compact) <= limit:
        return compact
    return compact[: limit - 1] + "…"
