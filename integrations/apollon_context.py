"""Intent classification and context assembly for the Apollon adapter."""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum
from typing import Callable, List, Tuple

from core.task_id_parser import TaskIdParser


class QueryIntent(Enum):
    """Query intent used to keep recall and knowledge lookup separated."""

    CONTEXT_RECALL = "context_recall"
    KNOWLEDGE_QUERY = "knowledge_query"
    UNKNOWN = "unknown"


class IntentClassifier:
    """Classify user messages into recall or knowledge lookup."""

    CONTEXT_KEYWORDS = [
        "上次",
        "之前",
        "刚才",
        "刚才说的",
        "早些时候",
        "昨天",
        "今天早些时候",
        "刚才的",
        "继续",
        "接着",
        "回到",
        "刚才那个",
        "之前那个",
        "回忆",
        "复盘",
        "回顾一下",
        "总结一下",
        "之前做过",
        "做到哪了",
        "进度",
        "状态",
        "聊天记录",
        "对话记录",
        "会话",
        "说过",
    ]

    KNOWLEDGE_KEYWORDS = [
        "是什么",
        "什么叫",
        "什么是",
        "定义",
        "概念",
        "解释一下",
        "介绍一下",
        "说明",
        "架构",
        "结构",
        "设计",
        "原理",
        "机制",
        "规则",
        "规范",
        "约定",
        "标准",
        "如何",
        "怎么",
        "怎样",
        "流程",
        "步骤",
        "方法",
        "方式",
        "做法",
        "实现",
        "为什么",
        "原理是什么",
        "底层",
        "核心",
        "关键点",
        "注意事项",
        "最佳实践",
        "系统",
        "框架",
        "模块",
        "组件",
        "接口",
    ]

    @staticmethod
    def _is_keyword_match(text: str, keyword: str) -> bool:
        """Use word boundaries for ASCII and substring matching for Chinese."""
        if re.search(r"[a-zA-Z0-9_]", keyword):
            return bool(re.search(r"\b" + re.escape(keyword) + r"\b", text))
        return keyword in text

    @classmethod
    def classify(cls, user_message: str) -> Tuple[QueryIntent, float, List[str]]:
        """Return intent, confidence, and the matched keywords."""
        if not user_message:
            return QueryIntent.UNKNOWN, 0.0, []

        message = user_message.lower()
        context_matches = [
            keyword
            for keyword in cls.CONTEXT_KEYWORDS
            if cls._is_keyword_match(message, keyword)
        ]
        knowledge_matches = [
            keyword
            for keyword in cls.KNOWLEDGE_KEYWORDS
            if cls._is_keyword_match(message, keyword)
        ]
        context_score = len(context_matches)
        knowledge_score = len(knowledge_matches)

        if context_score > 0 and knowledge_score == 0:
            return (
                QueryIntent.CONTEXT_RECALL,
                min(0.9, 0.5 + context_score * 0.1),
                context_matches,
            )
        if knowledge_score > 0 and context_score == 0:
            return (
                QueryIntent.KNOWLEDGE_QUERY,
                min(0.9, 0.5 + knowledge_score * 0.1),
                knowledge_matches,
            )
        if context_score > 0 and knowledge_score > 0:
            if context_score > knowledge_score * 1.5:
                return QueryIntent.CONTEXT_RECALL, 0.7, context_matches
            if knowledge_score > context_score * 1.5:
                return QueryIntent.KNOWLEDGE_QUERY, 0.7, knowledge_matches
            return (
                QueryIntent.CONTEXT_RECALL,
                0.6,
                context_matches + knowledge_matches,
            )
        return QueryIntent.UNKNOWN, 0.0, []


def detect_private_keywords(user_message: str) -> bool:
    """Return whether the request explicitly asks for private handling."""
    return TaskIdParser.is_private_request(user_message)


@dataclass(frozen=True)
class ContextProviders:
    """Runtime callbacks retained by Apollon's monkeypatch-compatible facade."""

    classify_intent: Callable[[str], Tuple[QueryIntent, float, List[str]]]
    detect_private_keywords: Callable[[str], bool]
    get_l1_context: Callable[..., str]
    get_wiki_knowledge: Callable[..., str | None]
    load_knowledge_in_action: Callable[[str], str]
    build_lightweight_preflight: Callable[[str, str, str], str]
    build_predictive_push_section: Callable[[str], str]
    build_observation_section: Callable[[], str]
    get_persona_behavior_prompt: Callable[[str | None], str]
    build_persona_section: Callable[..., str]


def build_context_for_agent(
    agent: str,
    working_dir: str,
    user_message: str,
    authorize_cross: List[str] | None,
    mode: str,
    providers: ContextProviders,
) -> str:
    """Assemble preflight context while enforcing source separation by intent."""
    if mode == "light":
        return providers.build_lightweight_preflight(
            agent, working_dir, user_message
        )

    intent, confidence, keywords = providers.classify_intent(user_message)
    if user_message and providers.detect_private_keywords(user_message):
        authorize_cross = []

    print(
        f"[Intent] 用户意图: {intent.value}, 置信度: {confidence:.2f}, "
        f"关键词: {keywords[:3]}"
    )
    context_parts = []

    if intent == QueryIntent.CONTEXT_RECALL:
        print("[Context] 判定为【上下文回忆类】，仅读取L1存储...")
        context_parts.append(
            providers.get_l1_context(
                working_dir,
                authorize_cross=authorize_cross,
                agent=agent,
            )
        )
    elif intent == QueryIntent.KNOWLEDGE_QUERY:
        print("[Context] 判定为【知识查询类】，仅检索Wiki...")
        wiki_context = providers.get_wiki_knowledge(user_message, agent=agent)
        context_parts.append(wiki_context or "\n（Wiki中未找到相关知识）\n")
    else:
        print("[Context] 意图不明确，保守策略：仅读取L1存储...")
        context_parts.append(
            providers.get_l1_context(
                working_dir,
                authorize_cross=authorize_cross,
                agent=agent,
            )
        )

    if intent != QueryIntent.CONTEXT_RECALL:
        kia_context = providers.load_knowledge_in_action(user_message)
        if kia_context:
            context_parts.append(kia_context)

    if intent != QueryIntent.CONTEXT_RECALL and user_message:
        push_context = providers.build_predictive_push_section(user_message)
        if push_context:
            context_parts.append(push_context)

    observation_context = providers.build_observation_section()
    if observation_context:
        context_parts.append(observation_context)

    if agent.lower() == "claude":
        persona_behavior = providers.get_persona_behavior_prompt(working_dir)
    else:
        persona_behavior = providers.build_persona_section(
            agent, working_dir=working_dir
        )
    if persona_behavior:
        context_parts.append(persona_behavior)

    if intent == QueryIntent.CONTEXT_RECALL:
        context_parts.append(
            f"\n<!-- Intent: {intent.value}, Confidence: {confidence:.2f}, "
            "ContainsContextRecall: true -->"
        )
    else:
        context_parts.append(
            f"\n<!-- Intent: {intent.value}, Confidence: {confidence:.2f} -->"
        )

    return "\n".join(context_parts)
