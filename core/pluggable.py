# -*- coding: utf-8 -*-
"""
PluggableModule 热插拔接口

ADR-012: 所有 L3 模块必须实现，L2 推荐实现，L1/L4 可选。
提供统一的 enable/disable/configure/handle_event 生命周期管理。

事件总线由调用方（如 EventBus 或 SyncEngine）维护，PluggableModule
本身不感知总线存在，只被动接收事件。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Dict, Any, Optional


class PluggableModule(ABC):
    """热插拔模块基类

    Usage:
        class MyModule(PluggableModule):
            def enable(self):
                self._active = True

            def disable(self):
                self._active = False

            def configure(self, cfg: Dict):
                self.threshold = cfg.get("threshold", 0.5)

            def handle_event(self, event_type: str, data: Dict):
                if event_type == "page_created" and self._active:
                    self.process(data["page"])
    """

    @abstractmethod
    def enable(self) -> None:
        """启用模块。可在此初始化资源、启动后台线程等。"""
        ...

    @abstractmethod
    def disable(self) -> None:
        """禁用模块。可在此释放资源、停止线程等。"""
        ...

    @abstractmethod
    def configure(self, cfg: Dict[str, Any]) -> None:
        """配置模块参数。

        Args:
            cfg: 配置字典，由调用方从 get_config() 或用户输入构造。
        """
        ...

    @abstractmethod
    def handle_event(self, event_type: str, data: Dict[str, Any]) -> None:
        """处理事件。

        事件类型规范（由调用方保证）：
        - task_completed: 任务完成
        - skill_executed: Skill 执行
        - skill_deviated: Skill 偏离
        - page_accessed: 页面被访问
        - page_created: 新页面入库
        - periodic_cleanup: 定期清理
        - periodic_stress_test: 定期压力测试
        - knowledge_needs_reinforcement: 知识需加固
        - profile_health_adjust: 画像健康度调整
        - profile_blindspot_detected: 盲区发现

        Args:
            event_type: 事件类型标识
            data: 事件数据字典
        """
        ...

    # ---- 事件发布辅助（可选，不强制实现）----

    def _emit_event(self, event_type: str, payload: Dict[str, Any]) -> Optional[str]:
        """向事件总线发布事件。

        如果事件总线未初始化，静默忽略（不抛异常）。
        返回值：trace_id 或 None。
        """
        try:
            from core.mnemos_bus import get_event_bus
            bus = get_event_bus()
            return bus.publish(event_type, payload=payload)
        except Exception:
            return None
