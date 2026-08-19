"""Minimal configuration protocol for dependency injection."""

from pathlib import Path
from typing import Any, Protocol


class ConfigProvider(Protocol):
    """配置提供协议。

    服务构造函数接受 ``config: Optional[ConfigProvider] = None``，默认回退到
    ``get_config()``。测试可注入 ``tests.conftest.FakeConfig`` 等替身，避免
    全局单例污染。
    """

    @property
    def data_dir(self) -> Path:
        ...

    @property
    def database_dir(self) -> Path:
        ...

    @property
    def wiki_dir(self) -> Path:
        ...

    @property
    def obsidian_vault_path(self) -> Path:
        ...

    def get(self, key: str, default: Any = None) -> Any:
        ...
