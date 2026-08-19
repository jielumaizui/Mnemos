# Prometheus Fire — 普罗米修斯之火
# 蒸馏任务数据结构定义

"""
职责：
- 定义 QueueDistillTask 数据结构，供 HephaestusWorker 使用。
- 作为 distill_queue → HephaestusWorker → DistillationEngine 路径的数据契约。

历史备注：
- 旧版 AgentDelegate 曾用于将蒸馏任务委托给宿主 Agent（同源复用模式）。
- 该模式因品质不可控（Agent 可能绕过管道自行处理）已退役。
- 当前所有蒸馏任务由 Mnemos 内部通过 LLM API 直接完成。
"""

from typing import Dict, List


class QueueDistillTask:
    """蒸馏任务结构（队列任务 DTO）"""

    def __init__(self, session_id: str, messages: List[Dict], meta: Dict):
        self.session_id = session_id
        self.messages = messages
        self.meta = meta

    def to_dict(self) -> Dict:
        return {
            "session_id": self.session_id,
            "messages": self.messages,
            "meta": self.meta,
        }
