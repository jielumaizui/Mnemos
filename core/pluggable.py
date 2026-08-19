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
from dataclasses import dataclass, field
from typing import Callable, Dict, Any, Optional

import logging

logger = logging.getLogger(__name__)

PLUGIN_OPERATION_ERRORS = (
    ImportError,
    OSError,
    RuntimeError,
    ValueError,
    TypeError,
    KeyError,
)

ModuleFactory = Callable[[], "PluggableModule"]


@dataclass(frozen=True)
class ModuleSpec:
    """Runtime registration metadata for a pluggable module."""

    module_id: str
    factory: ModuleFactory
    enabled: bool = True
    dependencies: tuple[str, ...] = field(default_factory=tuple)


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
        except (OSError, ValueError, TypeError, KeyError, ImportError, AttributeError, RuntimeError):
            logger.debug("事件发布失败（事件总线未初始化）", exc_info=True)
            return None


class ModuleRegistry:
    """Runtime registry for configurable PluggableModule instances."""

    def __init__(self):
        self._specs: Dict[str, ModuleSpec] = {}
        self._instances: Dict[str, PluggableModule] = {}
        self._status: Dict[str, Dict[str, Any]] = {}
        self._config: Dict[str, Any] = {}
        self._started_order: list[str] = []
        self._event_bridge_handler: Optional[Callable[[Any], Dict[str, Dict[str, Any]]]] = None

    def register(
        self,
        module_id: str,
        factory: ModuleFactory,
        *,
        enabled: bool = True,
        dependencies: list[str] | tuple[str, ...] = (),
    ) -> None:
        """Register a module factory and dependency metadata."""
        if not module_id:
            raise ValueError("module_id is required")
        if module_id in self._specs:
            raise ValueError(f"module already registered: {module_id}")
        self._specs[module_id] = ModuleSpec(
            module_id=module_id,
            factory=factory,
            enabled=enabled,
            dependencies=tuple(dependencies),
        )
        self._status[module_id] = {"state": "registered"}

    def configure(self, cfg: Dict[str, Any] | None) -> None:
        """Set registry configuration.

        Expected shape:
            {"modules": {"module_id": {"enabled": true, ...}}}
        """
        self._config = dict(cfg or {})

    def _module_config(self, module_id: str) -> Dict[str, Any]:
        modules_cfg = self._config.get("modules", {})
        if not isinstance(modules_cfg, dict):
            return {}
        module_cfg = modules_cfg.get(module_id, {})
        return dict(module_cfg) if isinstance(module_cfg, dict) else {}

    def _is_enabled(self, spec: ModuleSpec) -> bool:
        module_cfg = self._module_config(spec.module_id)
        if "enabled" in module_cfg:
            return bool(module_cfg["enabled"])
        return spec.enabled

    def _topological_order(self) -> list[str]:
        ordered: list[str] = []
        visiting: set[str] = set()
        visited: set[str] = set()

        def visit(module_id: str) -> None:
            if module_id in visited:
                return
            if module_id in visiting:
                raise ValueError(f"circular module dependency at {module_id}")
            visiting.add(module_id)
            spec = self._specs[module_id]
            for dependency in spec.dependencies:
                if dependency in self._specs:
                    visit(dependency)
            visiting.remove(module_id)
            visited.add(module_id)
            ordered.append(module_id)

        for module_id in self._specs:
            visit(module_id)
        return ordered

    def _dependency_block_reason(self, spec: ModuleSpec) -> str:
        for dependency in spec.dependencies:
            if dependency not in self._specs:
                return f"dependency {dependency} is missing"
            dependency_status = self._status.get(dependency, {})
            dependency_state = dependency_status.get("state")
            if dependency_state == "disabled":
                return f"dependency {dependency} is disabled"
            if dependency_state != "running":
                return f"dependency {dependency} is not running"
        return ""

    def _start_self(self, module_id: str) -> None:
        spec = self._specs[module_id]
        try:
            instance = self._instances.get(module_id)
            if instance is None:
                instance = spec.factory()
                self._instances[module_id] = instance
            instance.configure(self._module_config(module_id))
            instance.enable()
            if module_id not in self._started_order:
                self._started_order.append(module_id)
            self._status[module_id] = {"state": "running"}
        except PLUGIN_OPERATION_ERRORS as exc:
            self._status[module_id] = {
                "state": "failed",
                "error": str(exc),
            }
            logger.warning("Pluggable module start failed: %s", module_id, exc_info=True)

    def start_module(self, module_id: str) -> Dict[str, Dict[str, Any]]:
        """Start one module and its dependencies."""
        if module_id not in self._specs:
            raise KeyError(f"module is not registered: {module_id}")

        def start_recursive(current_id: str, stack: set[str]) -> None:
            if current_id in stack:
                raise ValueError(f"circular module dependency at {current_id}")
            current_status = self._status.get(current_id, {})
            if current_status.get("state") in {"running", "disabled", "failed", "blocked"}:
                return

            spec = self._specs[current_id]
            if not self._is_enabled(spec):
                self._status[current_id] = {"state": "disabled"}
                return

            stack.add(current_id)
            for dependency in spec.dependencies:
                if dependency not in self._specs:
                    self._status[current_id] = {
                        "state": "blocked",
                        "reason": f"dependency {dependency} is missing",
                    }
                    stack.remove(current_id)
                    return
                start_recursive(dependency, stack)
            stack.remove(current_id)

            block_reason = self._dependency_block_reason(spec)
            if block_reason:
                self._status[current_id] = {
                    "state": "blocked",
                    "reason": block_reason,
                }
                return
            self._start_self(current_id)

        start_recursive(module_id, set())
        return self.status()

    def start_enabled(self) -> Dict[str, Dict[str, Any]]:
        """Configure and enable all enabled modules in dependency order."""
        self._started_order = []
        for module_id in self._topological_order():
            self.start_module(module_id)
        return self.status()

    def get_instance(self, module_id: str) -> Optional[PluggableModule]:
        """Return a started or constructed module instance, if any."""
        return self._instances.get(module_id)

    def stop_all(self) -> Dict[str, Dict[str, Any]]:
        """Disable running modules in reverse start order."""
        stopped: Dict[str, Dict[str, Any]] = {}
        for module_id in reversed(self._started_order):
            instance = self._instances.get(module_id)
            if not instance:
                continue
            try:
                instance.disable()
                self._status[module_id] = {"state": "stopped"}
                stopped[module_id] = self._status[module_id]
            except PLUGIN_OPERATION_ERRORS as exc:
                self._status[module_id] = {
                    "state": "failed_stop",
                    "error": str(exc),
                }
                stopped[module_id] = self._status[module_id]
                logger.warning("Pluggable module stop failed: %s", module_id, exc_info=True)
        self._started_order = []
        return stopped

    def dispatch_event(self, event_type: str, data: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
        """Dispatch an event to running modules, isolating handler failures."""
        results: Dict[str, Dict[str, Any]] = {}
        for module_id in self._started_order:
            if self._status.get(module_id, {}).get("state") != "running":
                continue
            instance = self._instances[module_id]
            try:
                instance.handle_event(event_type, data)
                results[module_id] = {"state": "handled"}
            except PLUGIN_OPERATION_ERRORS as exc:
                results[module_id] = {"state": "failed", "error": str(exc)}
                logger.warning("Pluggable module event failed: %s", module_id, exc_info=True)
        return results

    def event_bus_handler(self) -> Callable[[Any], Dict[str, Dict[str, Any]]]:
        """Return an EventBus-compatible handler that dispatches to running modules."""
        if self._event_bridge_handler is not None:
            return self._event_bridge_handler

        def _module_registry_event_bridge(event: Any) -> Dict[str, Dict[str, Any]]:
            payload = getattr(event, "payload", {})
            if not isinstance(payload, dict):
                payload = {"payload": payload}
            return self.dispatch_event(getattr(event, "event_type", ""), payload)

        _module_registry_event_bridge.__name__ = f"module_registry_bridge_{id(self):x}"
        self._event_bridge_handler = _module_registry_event_bridge
        return _module_registry_event_bridge

    def subscribe_to_event_bus(
        self, event_bus: Any, event_type: str = "*"
    ) -> Callable[[Any], Dict[str, Dict[str, Any]]]:
        """Subscribe this registry to EventBus and return the bridge handler."""
        handler = self.event_bus_handler()
        event_bus.subscribe(event_type, handler)
        return handler

    def status(self) -> Dict[str, Dict[str, Any]]:
        """Return current registry status by module id."""
        return {module_id: dict(status) for module_id, status in self._status.items()}

    def health_check(self) -> Dict[str, Dict[str, Any]]:
        """Return health details for all registered modules."""
        health: Dict[str, Dict[str, Any]] = {}
        for module_id in self._specs:
            state = self._status.get(module_id, {}).get("state", "registered")
            instance = self._instances.get(module_id)
            details: Dict[str, Any] = {}
            if instance and hasattr(instance, "health_check"):
                try:
                    details = instance.health_check()  # type: ignore[attr-defined]
                except PLUGIN_OPERATION_ERRORS as exc:
                    details = {"healthy": False, "error": str(exc)}
            else:
                details = {"healthy": state == "running"}
            health[module_id] = {
                "state": state,
                "enabled": self._is_enabled(self._specs[module_id]),
                "dependencies": list(self._specs[module_id].dependencies),
                "details": details,
            }
        return health
