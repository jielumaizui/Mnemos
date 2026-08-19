"""Typed contracts for the in-process guard."""

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Dict, List, Optional, Set

from .prophasis import ChecklistItem


class GuardLevel(Enum):
    """守护级别"""

    SILENT = "silent"  # 轻微：静默记录
    HINT = "hint"  # 中等：自然融入
    INTERRUPT = "interrupt"  # 严重：打断确认


@dataclass
class ExecutionContext:
    """执行上下文：guard 检查时的任务状态

    让告警不再孤立——知道「当前在执行什么任务的哪一步」。
    """

    task_type: str  # 任务类型，如 "distill", "wiki_build"
    task_id: str  # 任务唯一标识
    step: str = ""  # 当前步骤，如 "hard_validate", "write_wiki"
    start_time: Optional[datetime] = None
    metadata: Dict = field(default_factory=dict)

    def __post_init__(self):
        if self.start_time is None:
            self.start_time = datetime.now()

    @property
    def elapsed_seconds(self) -> float:
        if self.start_time is None:
            return 0.0
        return (datetime.now() - self.start_time).total_seconds()


@dataclass
class GuardAlert:
    """守护告警"""

    level: GuardLevel
    checklist_item: ChecklistItem
    triggered_by: str  # 触发来源：user/ai
    trigger_text: str  # 触发文本
    suggestion: str  # 建议内容
    timestamp: str = ""
    execution_context: Optional[ExecutionContext] = None
    metadata: Dict[str, Any] = field(default_factory=dict)

    def __post_init__(self):
        if not self.timestamp:
            self.timestamp = datetime.now().isoformat()


@dataclass
class GuardSession:
    """守护会话状态"""

    task_type: str
    subtype: str
    checklist: List[ChecklistItem]
    triggered_alerts: List[GuardAlert] = field(default_factory=list)
    silent_records: List[Dict] = field(default_factory=list)
    hint_used: Set[str] = field(default_factory=set)
