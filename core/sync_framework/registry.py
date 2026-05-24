# -*- coding: utf-8 -*-
"""
AgentRegistry — 插件注册表

支持自动发现 + 手动注册。
启动时检查各 Agent 数据目录是否存在，存在的才实例化对应的 Source 类。

Usage:
    from core.sync_framework.registry import AgentRegistry
    from integrations.sources.claude_source import ClaudeSource

    AgentRegistry.register("claude", ClaudeSource)
    active = AgentRegistry.auto_discover()
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Dict, List, Optional, Type

from .agent_source import AgentSource


class AgentRegistry:
    """Agent 插件注册表"""

    _registry: Dict[str, Type[AgentSource]] = {}

    @classmethod
    def register(cls, name: str, source_class: Type[AgentSource]) -> None:
        """手动注册（用于内置 Agent）"""
        cls._registry[name] = source_class

    @classmethod
    def auto_discover(cls) -> List[AgentSource]:
        """
        自动发现：检查各 Agent 数据目录是否存在，
        存在的才实例化对应的 Source 类。
        """
        discovered: List[AgentSource] = []
        for name, source_class in cls._registry.items():
            try:
                source = source_class()
                data_dir = source.data_dir or PathDiscover.find(name)
                if data_dir and data_dir.exists():
                    discovered.append(source)
            except Exception:
                # 实例化失败（如配置缺失），跳过
                pass
        return discovered

    @classmethod
    def get(cls, name: str) -> Optional[AgentSource]:
        """获取已注册的 AgentSource 实例（需先 discover）"""
        source_class = cls._registry.get(name)
        if source_class:
            try:
                return source_class()
            except Exception:
                return None
        return None

    @classmethod
    def list_registered(cls) -> List[str]:
        """列出所有已注册的 Agent 名称"""
        return list(cls._registry.keys())

    @classmethod
    def register_builtin_agents(cls) -> None:
        """注册所有内置 Agent（在系统启动时调用）"""
        # 延迟导入避免循环依赖
        try:
            from integrations.sources.claude_source import ClaudeSource
            cls.register("claude", ClaudeSource)
        except ImportError:
            pass

        # TODO: 迁移完成后注册其他 Agent
        # from integrations.sources.kimi_source import KimiSource
        # cls.register("kimi", KimiSource)
        # from integrations.sources.hermes_source import HermesSource
        # cls.register("hermes", HermesSource)
        # from integrations.sources.openclaw_source import OpenClawSource
        # cls.register("openclaw", OpenClawSource)
        # from integrations.sources.codex_source import CodexSource
        # cls.register("codex", CodexSource)


class PathDiscover:
    """跨平台 Agent 数据目录发现"""

    AGENT_CONFIG = {
        "claude":   {"env": [],               "std": ["~/.claude"]},
        "kimi":     {"env": [],               "std": ["~/.kimi"]},
        "hermes":   {"env": [],               "std": ["~/.hermes"]},
        "openclaw": {"env": ["OPENCLAW_STATE_DIR"], "std": ["~/.openclaw"]},
        "codex":    {"env": ["CODEX_HOME", "XDG_CONFIG_HOME"], "std": ["~/.codex", "~/.config/codex"]},
    }

    @classmethod
    def find(cls, agent_name: str) -> Optional[Path]:
        """发现 Agent 数据目录"""
        # 1. 用户显式配置（预留）
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
                path = Path(val).expanduser()
                if path.exists():
                    return path

        # 3. 标准路径
        for std_path in cfg.get("std", []):
            path = Path(std_path).expanduser()
            if path.exists():
                return path

        return None

    @classmethod
    def _load_user_config(cls) -> Dict[str, str]:
        """加载用户显式配置（预留扩展点）"""
        # TODO: 从 ~/.mnemos/agent_paths.yaml 或 get_config() 读取
        return {}
