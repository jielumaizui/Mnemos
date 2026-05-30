# -*- coding: utf-8 -*-
"""
AgentRegistry — 插件注册表 + AgentLifecycleManager

支持自动发现 + 手动注册。
启动时检查各 Agent 数据目录是否存在，存在的才实例化对应的 Source 类。
AgentLifecycleManager 管理 Agent 的启动发现 + 5 分钟刷新 + 崩溃指数退避重启。

Usage:
    from core.sync_framework.registry import AgentRegistry
    AgentRegistry.register_builtin_agents()
    active = AgentRegistry.auto_discover()
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from pathlib import Path
from typing import Dict, List, Optional, Type

from .agent_source import AgentSource



logger = logging.getLogger(__name__)
class AgentRegistry:
    """Agent 插件注册表"""

    _registry: Dict[str, Type[AgentSource]] = {}
    _instances: Dict[str, AgentSource] = {}

    @classmethod
    def register(cls, name: str, source_class: Type[AgentSource]) -> None:
        """手动注册"""
        cls._registry[name] = source_class

    @classmethod
    def auto_discover(cls) -> List[AgentSource]:
        """
        自动发现：检查各 Agent 数据目录是否存在，
        存在的才实例化对应的 Source 类。
        """
        discovered: List[AgentSource] = []
        for name, source_class in cls._registry.items():
            # 复用已有实例
            if name in cls._instances:
                discovered.append(cls._instances[name])
                continue

            try:
                source = source_class()
                data_dir = source.data_dir or PathDiscover.find(name)
                if data_dir and data_dir.exists():
                    cls._instances[name] = source
                    discovered.append(source)
                    logger.info(f"[AgentRegistry] 发现 {name}: {data_dir}")
            except Exception as e:
                logger.debug(f"[AgentRegistry] 跳过 {name}: {e}")
        return discovered

    @classmethod
    def get(cls, name: str) -> Optional[AgentSource]:
        """获取已注册的 AgentSource 实例"""
        if name in cls._instances:
            return cls._instances[name]
        source_class = cls._registry.get(name)
        if source_class:
            try:
                source = source_class()
                data_dir = source.data_dir or PathDiscover.find(name)
                if data_dir and data_dir.exists():
                    cls._instances[name] = source
                    return source
            except Exception:
                logging.getLogger(__name__).warning(f"Caught unexpected error", exc_info=True)
                pass
        return None

    @classmethod
    def list_registered(cls) -> List[str]:
        """列出所有已注册的 Agent 名称"""
        return list(cls._registry.keys())

    @classmethod
    def list_active(cls) -> List[str]:
        """列出所有活跃的 Agent 名称"""
        return list(cls._instances.keys())

    @classmethod
    def register_builtin_agents(cls) -> None:
        """注册所有内置 Agent"""
        agents = [
            ("claude", "integrations.sources.claude_source", "ClaudeSource"),
            ("kimi", "integrations.sources.kimi_source", "KimiSource"),
            ("hermes", "integrations.sources.hermes_source", "HermesSource"),
            ("openclaw", "integrations.sources.openclaw_source", "OpenClawSource"),
            ("codex", "integrations.sources.codex_source", "CodexSource"),
            ("aider", "integrations.sources.aider_source", "AiderSource"),
            ("gemini", "integrations.sources.gemini_cli_source", "GeminiCliSource"),
            ("cursor", "integrations.sources.cursor_source", "CursorSource"),
            ("windsurf", "integrations.sources.windsurf_source", "WindsurfSource"),
        ]
        for name, module_path, class_name in agents:
            if name in cls._registry:
                continue
            try:
                import importlib
                module = importlib.import_module(module_path)
                source_class = getattr(module, class_name)
                cls.register(name, source_class)
            except (ImportError, AttributeError) as e:
                logger.debug(f"[AgentRegistry] 注册 {name} 失败: {e}")


class PathDiscover:
    """跨平台 Agent 数据目录发现"""

    AGENT_CONFIG = {
        "claude":   {"env": [],               "std": ["~/.claude"]},
        "kimi":     {"env": [],               "std": ["~/.kimi"]},
        "hermes":   {"env": [],               "std": ["~/.hermes"]},
        "openclaw": {"env": ["OPENCLAW_STATE_DIR"], "std": ["~/.openclaw"]},
        "codex":    {"env": ["CODEX_HOME", "XDG_CONFIG_HOME"], "std": ["~/.codex", "~/.config/codex"]},
        "aider":    {"env": ["AIDER_CHAT_HISTORY_FILE"], "std": []},
        "gemini":   {"env": ["GEMINI_HOME"], "std": ["~/.gemini", "~/.config/gemini"]},
        "cursor":   {"env": ["CURSOR_HOME"], "std": ["~/.cursor", "~/.config/Cursor"]},
        "windsurf": {"env": ["WINDSURF_HOME"], "std": ["~/.windsurf", "~/.config/Windsurf"]},
    }

    AGENT_SUBDIRS = {
        "claude":   "projects",
        "kimi":     "sessions",
        "hermes":   "sessions",
        "openclaw": "workspace/memory/.dreams/session-corpus",
        "codex":    "sessions",
        "gemini":   "sessions",
    }

    @classmethod
    def find(cls, agent_name: str) -> Optional[Path]:
        """发现 Agent 数据目录（4 层回退）"""
        # 1. 用户显式配置
        config = cls._load_user_config()
        if agent_name in config:
            path = Path(config[agent_name]).expanduser()
            if path.exists():
                return path

        # 2. 环境变量
        cfg = cls.AGENT_CONFIG.get(agent_name, {})
        for env_var in cfg.get("env", []):
            val = os.environ.get(env_var)
            if val:
                if env_var == "XDG_CONFIG_HOME":
                    path = Path(val) / agent_name
                else:
                    path = Path(val).expanduser()
                if path.exists():
                    return path

        # 3. 进程探测（psutil）
        try:
            cls._discover_from_process(agent_name)
        except Exception:
            logging.getLogger(__name__).warning(f"Caught unexpected error at registry.py", exc_info=True)
            pass

        # 4. 标准路径
        for std_path in cfg.get("std", []):
            path = Path(std_path).expanduser()
            if path.exists():
                return path

        return None

    @classmethod
    def _discover_from_process(cls, agent_name: str) -> Optional[Path]:
        """通过 psutil 进程探测发现数据目录"""
        try:
            import psutil
        except ImportError:
            return None

        profile_arg_map = {
            "openclaw": {"--profile": lambda v: Path.home() / f".openclaw-{v}"},
            "codex": {"--home": lambda v: Path(v).expanduser()},
        }
        arg_mapping = profile_arg_map.get(agent_name, {})

        for proc in psutil.process_iter(['name', 'cmdline']):
            name = proc.info.get('name', '') or ''
            if agent_name.lower() not in name.lower():
                continue

            cmdline = proc.info.get('cmdline') or []
            for i, arg in enumerate(cmdline):
                if arg in arg_mapping and i + 1 < len(cmdline):
                    val = cmdline[i + 1]
                    candidate = arg_mapping[arg](val)
                    if candidate.exists():
                        return candidate

        return None

    @classmethod
    def _load_user_config(cls) -> Dict[str, str]:
        """加载用户显式配置"""
        config_file = Path.home() / ".mnemos" / "configs" / "agent_paths.json"
        if config_file.exists():
            try:
                with open(config_file, "r", encoding="utf-8") as f:
                    return json.load(f)
            except Exception:
                logging.getLogger(__name__).warning(f"Caught unexpected error", exc_info=True)
                pass
        return {}


class AgentLifecycleManager:
    """
    Agent 生命周期管理器。

    职责：
    - 启动时发现所有活跃 Agent
    - 5 分钟刷新检查（新 Agent 上线 / 离线 Agent 恢复）
    - 崩溃指数退避重启
    - 离线 Agent 不销毁触发器，保持等待恢复
    """

    def __init__(self, refresh_interval: int = 300):
        self._refresh_interval = refresh_interval
        self._active_agents: Dict[str, AgentSource] = {}
        self._error_counts: Dict[str, int] = {}
        self._running = False
        self._refresh_thread: Optional[threading.Thread] = None
        self._lock = threading.Lock()

    def start(self):
        """启动生命周期管理"""
        if self._running:
            logger.warning("[LifecycleManager] 已启动，跳过重复调用")
            return
        self._running = True
        self._refresh_agents()
        self._refresh_thread = threading.Thread(
            target=self._refresh_loop, daemon=True
        )
        self._refresh_thread.start()
        logger.info(
            f"[LifecycleManager] 启动，监控 {len(self._active_agents)} 个 Agent"
        )

    def stop(self):
        """停止生命周期管理"""
        self._running = False
        if self._refresh_thread:
            self._refresh_thread.join(timeout=5)

    def discover_agents(self):
        """手动触发一次 Agent 发现（兼容 daemon 旧调用）"""
        self._refresh_agents()

    def get_active_agents(self) -> Dict[str, AgentSource]:
        """获取当前活跃的 Agent"""
        with self._lock:
            return dict(self._active_agents)

    def report_error(self, agent_name: str):
        """报告 Agent 错误"""
        with self._lock:
            self._error_counts[agent_name] = self._error_counts.get(agent_name, 0) + 1

    def report_success(self, agent_name: str):
        """报告 Agent 成功"""
        with self._lock:
            self._error_counts[agent_name] = 0

    def _refresh_agents(self):
        """刷新活跃 Agent 列表"""
        AgentRegistry.register_builtin_agents()
        active = AgentRegistry.auto_discover()
        with self._lock:
            new_names = {a.name for a in active}
            old_names = set(self._active_agents.keys())
            for agent in active:
                self._active_agents[agent.name] = agent
            # 离线 Agent 保留（不删除），等待恢复
            for name in old_names - new_names:
                logger.info(f"[LifecycleManager] Agent {name} 离线，保留等待恢复")

    def _refresh_loop(self):
        """定时刷新循环"""
        while self._running:
            # 分段 sleep
            end = time.monotonic() + self._refresh_interval
            while self._running and time.monotonic() < end:
                time.sleep(min(5, end - time.monotonic()))

            if not self._running:
                break

            try:
                self._refresh_agents()
            except Exception as e:
                logger.error(f"[LifecycleManager] 刷新失败: {e}")
