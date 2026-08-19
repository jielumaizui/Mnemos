# -*- coding: utf-8 -*-
"""
SyncFramework — 通用同步框架

canonical raw 摄入与投影层的核心基础设施。提供：
- AgentSource 接口：每个 Agent 只需实现 2 个方法 + 1 个配置项即可接入
- SyncEngine：统一协调层（8步流水线：增量跳过→噪音过滤→内容构建→脱敏→去重→标签组装→存储分片→信号采集）
- SourceRegistry：Source 插件注册与自动发现
- AgentLifecycleManager：Agent 生命周期管理
- TriggerDispatcher：触发策略统一抽象（watchdog/polling/hybrid）
- PathDiscover：跨平台路径发现（4层回退）
- FileIngestor：用户文件摄入（PDF/Word/PPT/Excel/HTML/epub/txt）

设计原则：
- 插件化：新 Agent 接入不改框架代码
- 单一写入：CaptureService → canonical raw；SyncEngine 不重复直写 raw vault
- 统一防重：一个 SQLite 库管所有 Agent
- 统一画像：同步过程中自动采集用户行为信号
"""

from .agent_source import AgentSource, SessionInfo, Turn, SyncResult
from .sync_engine import SyncEngine
from .registry import SourceRegistry, PathDiscover, AgentLifecycleManager

# 向后兼容别名
AgentRegistry = SourceRegistry
from .triggers import (  # noqa: E402
    TriggerDispatcher,
    WatchdogTrigger,
    PollingTrigger,
    HybridTrigger,
)  # noqa: E402
from .file_ingestor import FileIngestor  # noqa: E402
from .capture_queue import CaptureQueue  # noqa: E402
from .capture_worker import CaptureWorkerPool  # noqa: E402
from .capture_service import CaptureService  # noqa: E402

__all__ = [
    "AgentSource",
    "SessionInfo",
    "Turn",
    "SyncResult",
    "SyncEngine",
    "SourceRegistry",
    "AgentRegistry",  # 向后兼容别名
    "PathDiscover",
    "AgentLifecycleManager",
    "TriggerDispatcher",
    "WatchdogTrigger",
    "PollingTrigger",
    "HybridTrigger",
    "FileIngestor",
    "CaptureQueue",
    "CaptureWorkerPool",
    "CaptureService",
]
