# KIA Preflight — 统一的 KIA 预加载入口
#
# 职责：
# - 为所有 Agent 提供统一的 Knowledge-in-Action 预加载服务
# - 在 session.start 事件触发时，根据用户消息加载相关知识
# - 返回 Agent-agnostic 的上下文文本
#
# 设计原则：
# - 不依赖任何特定 Agent
# - 所有 Agent 通过此入口获取 KIA 上下文

from __future__ import annotations

import logging
from typing import Dict, List, Optional, Any

logger = logging.getLogger(__name__)


def run_preflight(agent: str, user_message: str, working_dir: str = "") -> str:
    """运行 KIA 预加载流程

    Args:
        agent: Agent 标识（claude/hermes/openclaw/opencode/codex）
        user_message: 用户输入消息
        working_dir: 当前工作目录

    Returns:
        KIA 上下文文本（Agent-agnostic）
    """
    context_parts = []

    try:
        # 1. 意图分类 + 知识加载
        from integrations.apollon import IntentClassifier, QueryIntent
        from integrations.apollon import get_wiki_knowledge, get_memos_context
        from integrations.apollon import load_knowledge_in_action

        intent, confidence, keywords = IntentClassifier.classify(user_message or "")
        logger.info(f"[Preflight] Agent={agent} intent={intent.value} confidence={confidence:.2f}")

        # 2. 根据意图选择数据源
        if intent == QueryIntent.CONTEXT_RECALL:
            memos_context = get_memos_context(working_dir, authorize_cross=None)
            context_parts.append(memos_context)

        elif intent == QueryIntent.KNOWLEDGE_QUERY:
            wiki_context = get_wiki_knowledge(user_message)
            if wiki_context:
                context_parts.append(wiki_context)
            else:
                context_parts.append("\n（Wiki中未找到相关知识）\n")

        elif intent == QueryIntent.UNKNOWN:
            memos_context = get_memos_context(working_dir, authorize_cross=None)
            context_parts.append(memos_context)

        # 3. Knowledge-in-Action（非上下文回忆类时）
        if intent != QueryIntent.CONTEXT_RECALL:
            kia_context = load_knowledge_in_action(user_message or "")
            if kia_context:
                context_parts.append(kia_context)

        # 4. 画像驱动行为策略
        persona_behavior = get_persona_behavior_prompt(agent)
        if persona_behavior:
            context_parts.append(persona_behavior)

        # 5. 添加意图标记
        context_parts.append(
            f"\n<!-- Intent: {intent.value}, Confidence: {confidence:.2f}, Agent: {agent} -->"
        )

    except Exception as e:
        logger.warning(f"[Preflight] KIA 预加载失败: {e}")
        return f"\n[KIA Preflight] 知识加载失败: {e}\n"

    return "\n".join(context_parts)


def get_persona_behavior_prompt(agent: str) -> str:
    """根据用户画像生成行为策略提示

    Args:
        agent: Agent 标识

    Returns:
        画像驱动的行为策略文本
    """
    try:
        from core.persona.delphi import get_behavior_prompt
        return get_behavior_prompt(agent)
    except Exception as e:
        logger.warning(f"[Preflight] 画像策略加载失败: {e}")
        return ""


def build_session_context(agent: str, working_dir: str, user_message: str) -> Dict[str, Any]:
    """构建完整的 session 上下文（供 session_start 使用）

    Returns:
        包含所有上下文部分的字典
    """
    return {
        "agent": agent,
        "working_dir": working_dir,
        "user_message": user_message,
        "kia_context": run_preflight(agent, user_message, working_dir),
    }
