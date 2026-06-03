# -*- coding: utf-8 -*-
"""
AgentSource 接口契约

每个 AI Agent 实现一个子类，接入 SyncFramework。
必须实现：name, model_tag, discover_sessions, parse_turns
可选覆写：data_dir, trigger_strategy, build_extra_tags, on_session_start, on_session_end
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Any


@dataclass
class SessionInfo:
    """可同步的会话信息"""
    session_id: str
    source_path: Path
    working_dir: Optional[str] = None
    mtime: Optional[float] = None


@dataclass
class Turn:
    """单轮对话记录 — 扩展以支持完整对话录入契约"""
    turn_number: int
    user_content: str
    assistant_content: str
    timestamp: Optional[str] = None
    metadata: Dict[str, Any] = field(default_factory=dict)
    # 完整录入契约字段（P0-0）
    tool_calls: List[Dict[str, Any]] = field(default_factory=list)
    tool_results: List[Dict[str, Any]] = field(default_factory=list)
    reasoning: str = ""
    attachments: List[Dict[str, Any]] = field(default_factory=list)
    raw_event_refs: List[Dict[str, Any]] = field(default_factory=list)
    source_files: List[str] = field(default_factory=list)
    completeness: Dict[str, Any] = field(default_factory=dict)

    def __post_init__(self):
        # 确保 completeness 有默认值
        if not self.completeness:
            self.completeness = {
                "visible_text": "full",
                "tool_results": "full" if self.tool_results else "unavailable",
                "reasoning": "full" if self.reasoning else "unavailable",
                "attachments": "full" if self.attachments else "unavailable",
                "truncated": False,
                "loss_reasons": [],
            }


@dataclass
class SyncResult:
    """同步结果"""
    session_id: str
    turn_number: int
    action: str  # "new" | "updated" | "skipped" | "noise" | "failed"
    memos_uids: List[str] = field(default_factory=list)
    content_hash: Optional[str] = None
    error: Optional[str] = None


@dataclass
class BatchSyncResult:
    """批量同步结果

    契约化 SyncEngine.sync_batch 的返回类型，替代裸 Dict。
    """
    agent: str
    total_sessions: int
    successful: List[Dict[str, Any]] = field(default_factory=list)
    failed: List[Dict[str, Any]] = field(default_factory=list)
    turn_stats: Dict[str, int] = field(default_factory=lambda: {
        "new": 0, "updated": 0, "skipped": 0, "noise": 0, "failed": 0
    })


class AgentSource(ABC):
    """Agent 数据源抽象，每个 AI 系统实现一个子类"""

    # ========== 必须实现 ==========

    @property
    @abstractmethod
    def name(self) -> str:
        """Agent 标识名，如 'claude', 'kimi', 'openclaw'"""
        ...

    @property
    @abstractmethod
    def model_tag(self) -> str:
        """模型标签，如 'claude-code', 'kimi-k2.5'"""
        ...

    @abstractmethod
    def discover_sessions(self) -> List[SessionInfo]:
        """发现当前所有可同步的会话"""
        ...

    @abstractmethod
    def parse_turns(self, session_path: Path) -> List[Turn]:
        """解析会话文件，提取按轮次排列的对话记录"""
        ...

    # ========== 可选覆写 ==========

    @property
    def data_dir(self) -> Optional[Path]:
        """
        Agent 数据目录。
        返回 None 时由框架通过 PathDiscover 自动探测。
        子类可覆写以提供精确路径，跳过自动发现。
        """
        return None

    @property
    def trigger_strategy(self) -> Dict[str, Any]:
        """
        声明触发策略，框架据此选择 TriggerDispatcher 实现。
        不覆写则默认：WatchdogTrigger + on_modified + 5s debounce
        """
        return {
            "type": "watchdog",
            "events": ["modified"],
            "debounce": 5.0,
            "recursive": True,
        }

    def build_extra_tags(self, turn: Turn) -> List[str]:
        """Agent 自定义标签"""
        return []

    def on_session_start(self, session_id: str, context: Dict[str, Any]) -> Dict[str, Any]:
        """KIA Hook：Session 开始时调用"""
        return {}

    def get_session_state(self, session_info: SessionInfo) -> Optional[Dict[str, Any]]:
        """
        返回 session 的聚合状态（多文件/数据库来源必须覆写）。
        用于 L1 扫描判断 session 是否变化，避免只看单个入口文件。

        返回 dict 必须包含：
          - mtime: 所有相关文件的最大 mtime
          - size: 所有相关文件的总大小（字节）
          - file_count: 相关文件数量
          - fingerprint: 可复现的哈希字符串
        """
        try:
            stat = session_info.source_path.stat()
            return {
                "mtime": session_info.mtime if session_info.mtime is not None else stat.st_mtime,
                "size": stat.st_size,
                "file_count": 1,
                "fingerprint": f"{session_info.source_path.name}:{stat.st_size}:{stat.st_mtime}",
            }
        except OSError:
            return None

    def completeness_capabilities(self) -> Dict[str, Any]:
        """
        声明该 AgentSource 理论上能采集到什么。
        用于 doctor/audit 显示来源完整性等级。
        """
        return {
            "visible_text": True,
            "tool_calls": False,
            "tool_results": False,
            "reasoning": "unknown",
            "attachments": "unknown",
            "raw_files": True,
            "source_fidelity": "full",
        }

    def on_session_end(self, session_id: str, messages: List[Dict[str, Any]]) -> None:
        """KIA Hook：Session 结束时调用"""
        # TODO: 子类可覆盖以实现 Session 结束时的清理/归档逻辑
        pass
