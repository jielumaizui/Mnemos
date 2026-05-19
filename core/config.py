"""
配置系统 - 统一管理所有路径和开关

优先级（高到低）：
1. 环境变量
2. 用户配置文件 (~/.config/mnemos/config.yaml)
3. 平台默认值
"""

import os
import sys
import yaml
from pathlib import Path
from typing import Dict, List, Optional
import logging

logger = logging.getLogger(__name__)


DEFAULT_CONFIG = {
    "wiki": {
        "vault_path": None,  # 自动检测平台
    },
    "memos": {
        "enabled": True,
        "api_url": "",
        "token": "",  # 优先从 MEMOS_TOKEN 环境变量读取
    },
    "persona": {
        "enabled": True,
        "data_sources": {
            "session": {"enabled": True, "description": "AI对话信号（核心）"},
            "git": {"enabled": False, "description": "Git提交记录"},
            "memos": {"enabled": False, "description": "Memos笔记"},
            "wiki": {"enabled": False, "description": "知识库交互"},
            "file_system": {"enabled": False, "description": "文件系统活动"},
            "wechat": {"enabled": False, "description": "微信聊天记录"},
        },
    },
    "cross_agent_share": True,  # 默认开启跨 Agent 知识共享
    "integrations": {
        "claude_code": {
            "enabled": True,
            "settings_json_path": None,  # 自动检测
        },
        "mcp": {
            "enabled": False,
        },
    },
}


class Config:
    """配置管理器"""

    def __init__(self, config_path: Optional[Path] = None):
        self.config_path = config_path or self._default_config_path()
        self._data = self._load()

    def _default_config_path(self) -> Path:
        if sys.platform == "win32":
            base = Path(os.environ.get("APPDATA", Path.home() / "AppData" / "Roaming"))
        elif sys.platform == "darwin":
            base = Path.home() / "Library" / "Application Support"
        else:
            base = Path.home() / ".config"
        return base / "mnemos" / "config.yaml"

    def _load(self) -> Dict:
        """加载配置：文件 + 环境变量覆盖"""
        data = self._apply_defaults({})

        # 1. 从文件加载
        if self.config_path.exists():
            try:
                with open(self.config_path, "r", encoding="utf-8") as f:
                    file_data = yaml.safe_load(f) or {}
                self._deep_merge(data, file_data)
            except Exception as e:
                logger.warning(f"忽略异常: {e}")

        # 2. 环境变量覆盖
        self._apply_env_overrides(data)

        # 3. 处理 None 值（自动检测）
        self._resolve_auto_values(data)

        return data

    def _apply_defaults(self, data: Dict) -> Dict:
        """应用默认配置"""
        import copy
        return copy.deepcopy(DEFAULT_CONFIG)

    def _deep_merge(self, base: Dict, override: Dict):
        """深度合并字典"""
        for key, val in override.items():
            if key in base and isinstance(base[key], dict) and isinstance(val, dict):
                self._deep_merge(base[key], val)
            else:
                base[key] = val

    def _apply_env_overrides(self, data: Dict):
        """环境变量覆盖配置"""
        # Memos
        if os.getenv("MEMOS_TOKEN"):
            data["memos"]["token"] = os.getenv("MEMOS_TOKEN")
        if os.getenv("MEMOS_API_URL"):
            data["memos"]["api_url"] = os.getenv("MEMOS_API_URL")

        # Wiki path
        if os.getenv("WIKI_DIR"):
            data["wiki"]["vault_path"] = os.getenv("WIKI_DIR")

        # Claude Code
        if os.getenv("CLAUDE_SETTINGS_JSON"):
            data["integrations"]["claude_code"]["settings_json_path"] = os.getenv("CLAUDE_SETTINGS_JSON")

    def _resolve_auto_values(self, data: Dict):
        """解析自动检测的值"""
        # Wiki vault path
        if data["wiki"]["vault_path"] is None:
            data["wiki"]["vault_path"] = str(self._default_wiki_path())

        # Claude Code settings.json
        cc_path = data["integrations"]["claude_code"]["settings_json_path"]
        if cc_path is None:
            data["integrations"]["claude_code"]["settings_json_path"] = str(self._default_claude_settings_path())

    def _default_wiki_path(self) -> Path:
        """根据平台自动检测默认 Wiki 路径"""
        if sys.platform == "darwin":
            return Path.home() / "Documents" / "Obsidian Vault" / "wiki"
        elif sys.platform == "win32":
            return Path.home() / "Documents" / "Obsidian Vault" / "wiki"
        else:
            return Path.home() / "wiki"

    def _default_claude_settings_path(self) -> Path:
        """自动检测 Claude Code settings.json 路径（跨平台）"""
        if sys.platform == "win32":
            return Path.home() / "AppData" / "Roaming" / "Claude" / "settings.json"
        elif sys.platform == "darwin":
            return Path.home() / "Library" / "Application Support" / "Claude" / "settings.json"
        else:
            return Path.home() / ".config" / "claude" / "settings.json"

    def save(self):
        """保存配置到文件"""
        self.config_path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.config_path, "w", encoding="utf-8") as f:
            yaml.dump(self._data, f, allow_unicode=True, sort_keys=False)

    # ---- 便捷访问方法 ----

    @property
    def wiki_dir(self) -> Path:
        return Path(self._data["wiki"]["vault_path"]).expanduser()

    @property
    def memos_enabled(self) -> bool:
        return self._data["memos"]["enabled"]

    @property
    def memos_token(self) -> str:
        return self._data["memos"]["token"]

    @property
    def memos_api_url(self) -> str:
        return self._data["memos"]["api_url"]

    @property
    def persona_enabled(self) -> bool:
        return self._data["persona"]["enabled"]

    @property
    def persona_data_sources(self) -> Dict:
        return self._data["persona"]["data_sources"]

    def is_source_enabled(self, source: str) -> bool:
        return self._data["persona"]["data_sources"].get(source, {}).get("enabled", False)

    @property
    def claude_code_enabled(self) -> bool:
        return self._data["integrations"]["claude_code"]["enabled"]

    @property
    def claude_settings_path(self) -> Path:
        return Path(self._data["integrations"]["claude_code"]["settings_json_path"]).expanduser()

    @property
    def mcp_enabled(self) -> bool:
        return self._data["integrations"]["mcp"]["enabled"]

    @property
    def cross_agent_share(self) -> bool:
        """是否默认开启跨 Agent 知识共享"""
        return self._data.get("cross_agent_share", True)

    @property
    def data_dir(self) -> Path:
        """Mnemos 运行时数据目录（数据库、状态文件等）"""
        return Path.home() / ".mnemos"

    @property
    def claude_data_dir(self) -> Path:
        """Claude Code 数据来源目录（distill_queue、wiki_state.db 等）

        跨平台路径：
        - macOS: ~/Library/Application Support/Claude/
        - Windows: %APPDATA%/Claude/ 或 ~/.claude（回退）
        - Linux: ~/.config/claude/
        """
        if sys.platform == "win32":
            p = Path(os.environ.get("APPDATA", Path.home() / "AppData" / "Roaming")) / "Claude"
            if p.exists():
                return p
            return Path.home() / ".claude"
        elif sys.platform == "darwin":
            p = Path.home() / "Library" / "Application Support" / "Claude"
            if p.exists():
                return p
            return Path.home() / ".claude"
        else:
            p = Path.home() / ".config" / "claude"
            if p.exists():
                return p
            return Path.home() / ".claude"

    def get(self, key: str, default=None):
        """按点号路径获取配置值"""
        keys = key.split(".")
        val = self._data
        for k in keys:
            if isinstance(val, dict) and k in val:
                val = val[k]
            else:
                return default
        return val

    def set(self, key: str, value):
        """按点号路径设置配置值"""
        keys = key.split(".")
        data = self._data
        for k in keys[:-1]:
            if k not in data:
                data[k] = {}
            data = data[k]
        data[keys[-1]] = value

    def to_dict(self) -> Dict:
        import copy
        return copy.deepcopy(self._data)


# 全局配置实例
_config: Optional[Config] = None


def get_config() -> Config:
    """获取全局配置实例"""
    global _config
    if _config is None:
        _config = Config()
    return _config


def reload_config():
    """重新加载配置"""
    global _config
    _config = Config()
