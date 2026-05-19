# Mnemos Event Bus — 统一事件总线
#
# 职责：
# - 提供跨 Agent / 跨进程的事件通信机制
# - 基于文件系统（无需网络、无依赖、跨平台）
# - 所有 Agent 适配器通过此总线发布和消费事件
# - Daemon 统一轮询并分发处理
#
# 设计原则：
# - Agent-Agnostic：事件格式不感知 Agent 类型
# - 轻量：纯文件系统操作，不引入消息队列依赖
# - 可靠：事件文件带状态标记（inbox → processing → archive）

from __future__ import annotations

import json
import logging
import os
import time
import uuid
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Any, Callable

logger = logging.getLogger(__name__)

# 事件目录根路径
EVENTS_ROOT = Path.home() / ".mnemos" / "events"


@dataclass
class Event:
    """标准事件格式"""

    event_id: str
    event_type: str       # session.start | session.end | distill.request | signal.batch | ...
    agent: str            # claude | hermes | openclaw | opencode | codex | daemon
    timestamp: str        # ISO 8601 with timezone
    payload: Dict[str, Any]
    status: str = "inbox"  # inbox | processing | archive

    @classmethod
    def from_file(cls, path: Path) -> Optional["Event"]:
        """从事件文件反序列化"""
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            return cls(**data)
        except Exception as e:
            logger.warning(f"读取事件文件失败 {path}: {e}")
            return None

    def to_dict(self) -> Dict:
        return asdict(self)

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, indent=2)


class EventBus:
    """统一事件总线

    基于文件系统的事件队列：
    ~/.mnemos/events/
    ├── inbox/        # 待处理事件
    ├── processing/   # 正在处理的事件
    └── archive/      # 已处理事件（按日期分目录）
    """

    def __init__(self, root_dir: Optional[Path] = None):
        self.root = root_dir or EVENTS_ROOT
        self._ensure_dirs()

    def _ensure_dirs(self):
        """确保事件目录结构存在"""
        for sub in ["inbox", "processing", "archive"]:
            (self.root / sub).mkdir(parents=True, exist_ok=True)

    # ========== 发布事件 ==========

    def publish(self, event_type: str, agent: str, payload: Dict[str, Any]) -> str:
        """发布事件到 inbox

        Args:
            event_type: 事件类型
            agent: 来源 Agent
            payload: 事件载荷

        Returns:
            事件 ID
        """
        event_id = str(uuid.uuid4())[:16]
        event = Event(
            event_id=event_id,
            event_type=event_type,
            agent=agent,
            timestamp=datetime.now(timezone.utc).isoformat(),
            payload=payload,
            status="inbox",
        )
        event_path = self.root / "inbox" / f"{event.timestamp[:19].replace(':', '-')}-{agent}-{event_type}-{event_id}.json"
        event_path.write_text(event.to_json(), encoding="utf-8")
        logger.info(f"[EventBus] 发布事件: {event_type} from {agent} id={event_id}")
        return event_id

    # ========== 消费事件 ==========

    def poll(self, event_types: Optional[List[str]] = None, limit: int = 100) -> List[Event]:
        """轮询 inbox 中的待处理事件

        Args:
            event_types: 过滤的事件类型列表（None = 全部）
            limit: 最大返回数量

        Returns:
            事件列表（按时间升序）
        """
        inbox_dir = self.root / "inbox"
        if not inbox_dir.exists():
            return []

        events = []
        for path in sorted(inbox_dir.glob("*.json")):
            event = Event.from_file(path)
            if event is None:
                continue
            if event_types and event.event_type not in event_types:
                continue
            events.append(event)
            if len(events) >= limit:
                break

        return events

    def ack(self, event_id: str) -> bool:
        """确认事件已处理，移入 archive

        Args:
            event_id: 事件 ID

        Returns:
            是否成功
        """
        # 查找事件（可能在 inbox 或 processing）
        for src_dir in ["inbox", "processing"]:
            src = self.root / src_dir
            if not src.exists():
                continue
            for path in src.glob(f"*{event_id}.json"):
                try:
                    # 按日期归档
                    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
                    archive_dir = self.root / "archive" / today
                    archive_dir.mkdir(parents=True, exist_ok=True)
                    dst = archive_dir / path.name
                    path.rename(dst)
                    logger.info(f"[EventBus] 归档事件: {event_id}")
                    return True
                except Exception as e:
                    logger.warning(f"归档事件失败 {event_id}: {e}")
                    return False
        logger.warning(f"[EventBus] 未找到事件: {event_id}")
        return False

    def move_to_processing(self, event_id: str) -> bool:
        """将事件从 inbox 移到 processing"""
        inbox_dir = self.root / "inbox"
        if not inbox_dir.exists():
            return False
        for path in inbox_dir.glob(f"*{event_id}.json"):
            try:
                dst = self.root / "processing" / path.name
                path.rename(dst)
                return True
            except Exception as e:
                logger.warning(f"移动事件到 processing 失败 {event_id}: {e}")
                return False
        return False

    # ========== 统计信息 ==========

    def stats(self) -> Dict[str, int]:
        """返回各状态事件数量"""
        result = {}
        for sub in ["inbox", "processing"]:
            d = self.root / sub
            result[sub] = len(list(d.glob("*.json"))) if d.exists() else 0
        # archive 按子目录计数
        archive_dir = self.root / "archive"
        result["archive"] = sum(
            len(list(d.glob("*.json")))
            for d in archive_dir.rglob(".") if d != archive_dir
        ) if archive_dir.exists() else 0
        return result


# ============================================================
# Event Processor — 事件处理器（Daemon 使用）
# ============================================================

class EventProcessor:
    """事件处理器 — 根据事件类型分发处理"""

    def __init__(self):
        self.bus = EventBus()
        self._handlers: Dict[str, Callable[[Event], Any]] = {}

    def register(self, event_type: str, handler: Callable[[Event], Any]):
        """注册事件处理器"""
        self._handlers[event_type] = handler
        logger.info(f"[EventProcessor] 注册处理器: {event_type}")

    def process_one(self, event: Event) -> Any:
        """处理单个事件"""
        handler = self._handlers.get(event.event_type)
        if not handler:
            logger.warning(f"[EventProcessor] 未找到处理器: {event.event_type}")
            return None

        # 移到 processing
        self.bus.move_to_processing(event.event_id)

        try:
            result = handler(event)
            # 处理完成，归档
            self.bus.ack(event.event_id)
            return result
        except Exception as e:
            logger.error(f"[EventProcessor] 处理事件失败 {event.event_id}: {e}")
            # 失败不移除，保留在 processing 中以便重试
            return None

    def process_all(self, event_types: Optional[List[str]] = None, limit: int = 50) -> int:
        """处理所有待处理事件

        Returns:
            处理的事件数量
        """
        events = self.bus.poll(event_types=event_types, limit=limit)
        if not events:
            return 0

        count = 0
        for event in events:
            self.process_one(event)
            count += 1

        return count


# ============================================================
# 便捷函数
# ============================================================

def publish_event(event_type: str, agent: str, payload: Dict[str, Any]) -> str:
    """便捷函数：发布事件"""
    bus = EventBus()
    return bus.publish(event_type, agent, payload)


def get_pending_events(event_types: Optional[List[str]] = None, limit: int = 100) -> List[Event]:
    """便捷函数：获取待处理事件"""
    bus = EventBus()
    return bus.poll(event_types=event_types, limit=limit)


def get_event_stats() -> Dict[str, int]:
    """便捷函数：获取事件统计"""
    bus = EventBus()
    return bus.stats()
