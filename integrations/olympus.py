# Olympus — 奥林匹斯，众神基座
# Agent 抽象基类，统一所有 AI Agent 的生命周期接口

from abc import ABC, abstractmethod
from typing import Dict, List, Optional, Any
from pathlib import Path


import logging
logger = logging.getLogger(__name__)
class AgentAdapter(ABC):
    """AI Agent 适配器基类

    所有 Agent 适配器必须实现此接口，Mnemos 通过此接口与各类 Agent 交互，
    无需关心底层 Agent 的具体实现差异。
    """

    @property
    @abstractmethod
    def name(self) -> str:
        """Agent 标识名，如 'claude', 'codex', 'hermes', 'openclaw', 'opencode'"""
        ...

    @property
    @abstractmethod
    def priority(self) -> int:
        """优先级数值，越小越优先（Claude Code=1, Hermes=2, OpenClaw=3, Codex=4, OpenCode=5）"""
        ...

    @abstractmethod
    def is_available(self) -> bool:
        """检测当前系统是否安装了此 Agent"""
        ...

    @abstractmethod
    def on_session_start(self, working_dir: str, user_message: str = "") -> Dict[str, Any]:
        """会话开始时调用

        Returns:
            信号采集结果，如 {"session_id": "...", "signals": [...]}
        """
        ...

    @abstractmethod
    def on_session_end(
        self, working_dir: str, session_messages: List[Dict] = None
    ) -> Dict[str, Any]:
        """会话结束时调用

        Args:
            session_messages: 本次会话的完整消息列表

        Returns:
            处理结果，如 {"queued": True, "distill_task_id": "..."}
        """
        ...

    @abstractmethod
    def install_hooks(self) -> bool:
        """安装 Agent 的 session hooks（如 Claude Code 的 settings.json）

        Returns:
            是否安装成功
        """
        ...

    @abstractmethod
    def collect_signals(self, days: int = 7) -> List[Dict]:
        """从 Agent 的历史记录中采集用户行为信号

        Args:
            days: 采集最近几天的数据

        Returns:
            信号列表
        """
        ...

    @abstractmethod
    def inject_knowledge(
        self, task_type: str, subtype: str = "", context_text: str = ""
    ) -> Dict[str, Any]:
        """向 Agent 注入知识（KIA 闭环第一步）

        默认通过 MCP 协议实现，各 Agent 可覆盖。
        """
        ...

    @abstractmethod
    def delegate_distillation(
        self, task_path: Path, output_path: Path
    ) -> bool:
        """委托 Agent 执行蒸馏任务（同源复用原则）

        Args:
            task_path: 蒸馏任务文件路径（JSON，包含原始对话）
            output_path: Agent 应将蒸馏结果写入的路径

        Returns:
            是否成功下发任务（不保证 Agent 已完成）
        """
        ...

    def get_config_path(self) -> Optional[Path]:
        """返回 Agent 的配置文件路径（可选覆盖）"""
        return None

    def get_data_dir(self) -> Optional[Path]:
        """返回 Agent 的数据目录（可选覆盖）"""
        return None

    def is_hooks_installed(self) -> bool:
        """检查 hooks 是否已安装。

        子类应覆盖此方法以提供适配器特定的验证逻辑。
        默认返回 False（未知状态）。
        """
        return False

    def install_mcp_server(self) -> bool:
        """安装 Mnemos MCP server 配置。

        hooks/wrappers 负责生命周期事件，MCP 负责让宿主 Agent 主动调用
        preflight_inject、guard_check、wiki_search 等工具。默认返回 False，
        子类按各自配置格式覆盖。
        """
        return False

    def is_mcp_configured(self) -> bool:
        """检查 Mnemos MCP server 是否已配置。"""
        return False

    def is_active_connection_installed(self) -> bool:
        """检查主动接入是否就绪。

        主动接入至少需要生命周期 hook/wrapper 和 MCP 工具配置同时存在。
        被动采集不由此状态表示。
        """
        return self.is_hooks_installed() and self.is_mcp_configured()


class AgentRegistry:
    """Agent 注册表 — 自动发现和管理所有适配器"""

    _adapters: List[type] = []

    @classmethod
    def register(cls, adapter_class: type):
        """注册 Agent 适配器类"""
        cls._adapters.append(adapter_class)
        return adapter_class

    @classmethod
    def discover_all(cls) -> List[AgentAdapter]:
        """发现本地所有可用的 Agent 适配器"""
        # 确保所有适配器模块已加载（触发 register）
        cls._ensure_adapters_loaded()
        instances = []
        for adapter_class in cls._adapters:
            try:
                inst = adapter_class()
                if inst.is_available():
                    instances.append(inst)
            except Exception:
                logging.getLogger(__name__).warning(f"Caught unexpected error at olympus.py", exc_info=True)
                continue
        # 按优先级排序
        instances.sort(key=lambda a: a.priority)
        return instances

    @classmethod
    def _ensure_adapters_loaded(cls):
        """显式导入所有适配器模块以触发注册"""
        adapter_modules = [
            "integrations.apollon",
            "integrations.caduceus",
            "integrations.daedalus",
            "integrations.musae",
            "integrations.typhon",
            "integrations.kimi_adapter",
        ]
        for mod_name in adapter_modules:
            try:
                __import__(mod_name)
            except Exception:
                logging.getLogger(__name__).warning(f"Caught unexpected error", exc_info=True)
                pass
    @classmethod
    def get_host_agent(cls) -> Optional[AgentAdapter]:
        """根据 MNEMOS_HOST_AGENT 环境变量返回宿主 Agent"""
        import os
        host = os.environ.get("MNEMOS_HOST_AGENT", "").lower()
        if not host:
            return None
        for inst in cls.discover_all():
            if inst.name.lower() == host:
                return inst
        return None

    @classmethod
    def select_best_agent(cls) -> Optional[AgentAdapter]:
        """选择最佳 Agent：先尝试同源复用，再按优先级扫描本地"""
        # 1. 同源复用
        host = cls.get_host_agent()
        if host:
            return host
        # 2. 按优先级扫描本地
        available = cls.discover_all()
        if available:
            return available[0]
        return None
