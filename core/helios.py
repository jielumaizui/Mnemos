# Helios — 赫利俄斯，太阳神
# 照亮一切 — Agent 检测器，发现本地可用的 AI Agent

"""
职责：
- 检测 MNEMOS_HOST_AGENT 环境变量（同源复用）
- 扫描本地安装的 Agent
- 按优先级返回最佳 Agent

此模块是对 olympus.AgentRegistry 的上层封装，
提供更简洁的检测接口。
"""

import os
import logging
from typing import Optional

from integrations.olympus import AgentRegistry, AgentAdapter

logger = logging.getLogger(__name__)


class AgentDetector:
    """Agent 检测器 — 赫利俄斯之眼"""

    def detect_host_agent(self) -> Optional[AgentAdapter]:
        """检测宿主 Agent（同源复用原则）

        谁启动 Mnemos，就用谁执行蒸馏。
        通过 MNEMOS_HOST_AGENT 环境变量识别。
        """
        return AgentRegistry.get_host_agent()

    def scan_local(self):
        """扫描本地所有可用 Agent

        Returns:
            按优先级排序的 Agent 列表
        """
        return AgentRegistry.discover_all()

    def select_best(self) -> Optional[AgentAdapter]:
        """选择最佳 Agent

        策略：
        1. 同源复用（MNEMOS_HOST_AGENT）
        2. 回退到本地扫描 + 优先级排序
        """
        return AgentRegistry.select_best_agent()

    def get_status_report(self) -> dict:
        """生成 Agent 状态报告"""
        host = self.detect_host_agent()
        all_agents = self.scan_local()

        return {
            "host_agent": host.name if host else None,
            "available_count": len(all_agents),
            "available": [a.name for a in all_agents],
            "best": all_agents[0].name if all_agents else None,
        }


def get_detector() -> AgentDetector:
    """获取全局 AgentDetector 实例"""
    return AgentDetector()
