# -*- coding: utf-8 -*-
"""
SyncFramework — 通用同步框架

L1 原始层的核心基础设施。提供：
- AgentSource 接口：每个 Agent 只需实现 2 个方法 + 1 个配置项即可接入
- SyncEngine：统一协调层（防重、过滤、构建、标签、分片、存储、信号采集）
- AgentRegistry：插件注册与自动发现
- TriggerDispatcher：触发策略统一抽象（watchdog/polling/hybrid）

设计原则：
- 插件化：新 Agent 接入不改框架代码
- 统一出口：所有数据经 SyncEngine → MemosClient
- 统一防重：一个 SQLite 库管所有 Agent
- 统一画像：同步过程中自动采集用户行为信号
"""

from .agent_source import AgentSource, SessionInfo, Turn, SyncResult
from .sync_engine import SyncEngine
from .registry import AgentRegistry

__all__ = [
    "AgentSource",
    "SessionInfo",
    "Turn",
    "SyncResult",
    "SyncEngine",
    "AgentRegistry",
]
