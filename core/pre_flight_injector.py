"""
PreFlightInjector — 预飞注入器

【E14 全库修复】蒸馏前向 LLM prompt 注入画像上下文，
让 LLM 了解用户画像后再做蒸馏判断，提升蒸馏精准度。

设计来源：00-架构总览.md（L4 应用层）、02-同步层/02-各Agent数据格式参考.md
"""

import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional

from core.config import get_config

logger = logging.getLogger(__name__)


@dataclass
class PersonaContext:
    """画像上下文"""
    tech_stack: List[str]           # 技术栈偏好
    work_hours: str                 # 工作时段
    style: str                      # 回复风格偏好
    blindspots: List[str]           # 已知盲区
    interaction_mode: str           # 交互模式


class PreFlightInjector:
    """
    预飞注入器

    在调用 LLM 进行蒸馏判断前，从用户画像系统读取画像摘要，
    注入到 system prompt 中，让 LLM "了解用户"后再做判断。
    """

    def __init__(self, persona_dir: Path = None):
        self.persona_dir = persona_dir or (get_config().data_dir / "persona")

    def build_system_prompt(self, agent_name: str = "") -> str:
        """
        构建带画像上下文的 system prompt。

        Returns:
            system prompt 字符串（可直接传给 host_agent_caller）
        """
        ctx = self._load_persona_context()
        lines = [
            "你是一位知识蒸馏助手，负责从用户的对话中提取有价值的知识并整理成笔记。",
            "",
            "【用户画像摘要】",
        ]

        if ctx.tech_stack:
            lines.append(f"- 技术栈偏好：{', '.join(ctx.tech_stack)}")
        if ctx.work_hours:
            lines.append(f"- 工作时段：{ctx.work_hours}")
        if ctx.style:
            lines.append(f"- 回复风格偏好：{ctx.style}")
        if ctx.blindspots:
            lines.append(f"- 已知盲区（需特别关注）：{', '.join(ctx.blindspots)}")
        if ctx.interaction_mode:
            lines.append(f"- 交互模式：{ctx.interaction_mode}")

        lines.extend([
            "",
            "【蒸馏原则】",
            "1. 优先提取与用户技术栈相关的知识",
            "2. 用户盲区领域的内容要额外标注提醒",
            "3. 根据用户风格偏好调整输出格式",
            "4. 工作时段外的内容可能是临时讨论，降低优先级",
        ])

        return "\n".join(lines)

    def inject_to_prompt(self, user_prompt: str,
                         agent_name: str = "") -> Dict[str, str]:
        """
        将画像上下文注入到用户 prompt 中。

        Returns:
            {"system": system_prompt, "user": user_prompt}
        """
        return {
            "system": self.build_system_prompt(agent_name),
            "user": user_prompt,
        }

    def _load_persona_context(self) -> PersonaContext:
        """从画像目录加载用户画像摘要"""
        default = PersonaContext(
            tech_stack=[],
            work_hours="",
            style="",
            blindspots=[],
            interaction_mode="",
        )

        summary_path = self.persona_dir / "profile_summary.json"
        if not summary_path.exists():
            return default

        try:
            data = json.loads(summary_path.read_text(encoding="utf-8"))
            return PersonaContext(
                tech_stack=data.get("tech_stack", []),
                work_hours=data.get("work_hours", ""),
                style=data.get("style", ""),
                blindspots=data.get("blindspots", []),
                interaction_mode=data.get("interaction_mode", ""),
            )
        except Exception as e:
            logger.warning(f"加载画像摘要失败: {e}")
            return default
