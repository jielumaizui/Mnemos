# -*- coding: utf-8 -*-
"""
BlindspotResponseBuilder — 为宿主 Agent 构建盲区提醒的自然语言回复

原则：
- 一句话说明检测到了什么
- 给用户一个明确的二元选择
- 不离开当前对话
- 不强行搜索（等用户确认）
"""

from typing import Dict, Optional

from core.app.blindspot_discovery import BlindSpotReminder


class BlindspotResponseBuilder:
    """构建盲区提醒响应"""

    # 用户可能的确认表述（供宿主 Agent 做意图识别）
    CONFIRM_PATTERNS = {
        "记录",
        "查",
        "查一下",
        "搜索",
        "搜一下",
        "好",
        "好的",
        "可以",
        "yes",
        "y",
        "确认",
    }

    IGNORE_PATTERNS = {
        "忽略",
        "不用",
        "不需要",
        "否",
        "不",
        "no",
        "n",
        "算了",
    }

    @classmethod
    def build_prompt(
        cls,
        reminder: BlindSpotReminder,
        suggested_query: str = "",
    ) -> str:
        """
        构建给宿主 Agent 展示的自然语言提示。

        Args:
            reminder: 盲点提醒对象
            suggested_query: 建议 AI 搜索用的查询词

        Returns:
            自然语言提示文本
        """
        topic = reminder.topic
        description = reminder.description

        # 中文 topic 与英文 topic 的展示处理
        if any("\u4e00" <= c <= "\u9fff" for c in topic):
            topic_display = topic
        else:
            topic_display = topic.replace("_", " ")

        lines = [
            f"我注意到你提到了 **{topic_display}**，当前授权知识范围里还没有足够资料。",
            "",
        ]

        if description and description != f"知识库中缺少关于「{topic}」的记录":
            lines.append(f"{description}")
            lines.append("")

        if suggested_query and suggested_query != topic:
            lines.append(f"要我查一下「{suggested_query}」的资料，然后继续讨论吗？")
        else:
            lines.append(f"要我查一下 **{topic_display}** 的资料，然后继续讨论吗？")

        lines.append("")
        lines.append("你可以直接回复：查一下 / 不用")

        return "\n".join(lines)

    @classmethod
    def build_tool_result(
        cls,
        reminder: Optional[BlindSpotReminder],
        suggested_query: str = "",
        degraded: bool = False,
        degraded_reasons: Optional[list] = None,
    ) -> Dict:
        """
        构建给宿主 Agent 的结构化结果。

        Returns:
            {
                "blindspot_found": bool,
                "topic": str,
                "description": str,
                "confidence": float,
                "reminded_at": str,
                "suggested_query": str,
                "prompt_for_user": str,  # 可直接展示给用户的自然语言
                "expected_user_actions": ["search", "ignore"],
                "degraded": bool,
                "degraded_reasons": [...],
            }
        """
        if reminder is None:
            return {
                "blindspot_found": False,
                "asset_type": "knowledge_coverage_gap",
                "topic": "",
                "description": "",
                "confidence": 0.0,
                "suggested_query": suggested_query,
                "prompt_for_user": "",
                "expected_user_actions": [],
                "degraded": degraded,
                "degraded_reasons": degraded_reasons or [],
            }

        return {
            "blindspot_found": True,
            "asset_type": reminder.asset_type,
            "asset_id": reminder.asset_id,
            "revision_id": reminder.revision_id,
            "topic": reminder.topic,
            "description": reminder.description,
            "confidence": round(reminder.confidence, 2),
            "reminded_at": reminder.reminded_at or "",
            "suggested_query": suggested_query,
            "prompt_for_user": cls.build_prompt(reminder, suggested_query),
            "expected_user_actions": ["search", "ignore"],
            "degraded": degraded,
            "degraded_reasons": degraded_reasons or [],
        }

    @classmethod
    def is_confirm(cls, user_reply: str) -> bool:
        """判断用户回复是否为确认。"""
        text = user_reply.strip().lower()
        return text in cls.CONFIRM_PATTERNS or any(text.startswith(p) for p in cls.CONFIRM_PATTERNS)

    @classmethod
    def is_ignore(cls, user_reply: str) -> bool:
        """判断用户回复是否为忽略。"""
        text = user_reply.strip().lower()
        return text in cls.IGNORE_PATTERNS or any(text.startswith(p) for p in cls.IGNORE_PATTERNS)
