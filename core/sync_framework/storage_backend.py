# -*- coding: utf-8 -*-
"""
StorageBackend — 存储后端抽象层

为 SyncEngine 提供统一的存储接口，屏蔽底层是 L1 storage 还是本地 Obsidian 文件的差异。

设计原则：
- SyncEngine 只认 StorageBackend，不感知具体后端实现
- 每个后端自行处理分片/不分片策略
- 返回值统一为 StorageResult，包含足够元数据用于去重和审计
"""

from __future__ import annotations

import importlib
import os
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Callable, List, Dict, Optional, Any
import logging

logger = logging.getLogger(__name__)


@dataclass
class StorageResult:
    """统一存储结果，后端无关"""

    uid: str  # 全局唯一标识（Obsidian 用文件路径，backend 用 uid）
    content: str  # 存储的原始内容（或摘要）
    tags: List[str] = field(default_factory=list)
    metadata: Dict[str, Any] = field(default_factory=dict)  # 后端特定元数据
    created_at: Optional[str] = None
    updated_at: Optional[str] = None


class StorageBackend(ABC):
    """存储后端抽象基类"""

    @abstractmethod
    def save(self, content: str, tags: List[str], title: str) -> List[StorageResult]:
        """
        保存内容。

        后端自行决定是否需要分片。返回一个或多个 StorageResult。
        对于 Obsidian 等无长度限制的后端，通常返回单个结果。
        对于 L1 storage 等有限制的后端，可能返回分片后的多个结果。

        Args:
            content: Markdown 内容（已脱敏）
            tags: 标签列表（格式为 "key=value" 或纯标签名）
            title: 标题（用于生成文件名或 Memo 标题）

        Returns:
            List[StorageResult] — 存储结果列表（至少一个）
        """
        ...

    @abstractmethod
    def search(self, query: str, limit: Optional[int] = None) -> List[StorageResult]:
        """
        全文搜索。

        Args:
            query: 搜索关键词
            limit: 最大返回数量

        Returns:
            List[StorageResult]
        """
        ...

    @abstractmethod
    def list_by_tags(self, tags: List[str], limit: Optional[int] = None) -> List[StorageResult]:
        """
        按标签查询。

        Args:
            tags: 必须全部匹配的标签列表（AND 逻辑）
            limit: 最大返回数量，None 表示无限制

        Returns:
            List[StorageResult]
        """
        ...

    @abstractmethod
    def get_by_id(self, uid: str) -> Optional[StorageResult]:
        """
        按唯一标识获取单条记录。

        Args:
            uid: save() 返回的 uid

        Returns:
            StorageResult 或 None
        """
        ...

    @abstractmethod
    def health_check(self) -> Dict[str, Any]:
        """
        健康检查。

        Returns:
            {"status": "ok"|"degraded"|"error", "message": str, ...}
        """
        ...

    @abstractmethod
    def update_tags(
        self,
        uid: str,
        add_tags: Optional[List[str]] = None,
        remove_tags: Optional[List[str]] = None,
    ) -> Optional[StorageResult]:
        """
        增量更新已有记录的标签。

        子类必须实现此方法，否则蒸馏成功后无法标记 ``status=distilled``，
        会导致同一 session 被重复处理。

        对于 Obsidian 等文件系统后端，需要读取文件、修改 frontmatter、写回。
        对于 API 后端，可调用原生标签更新接口。

        Args:
            uid: save() 返回的唯一标识
            add_tags: 要添加的标签列表
            remove_tags: 要移除的标签列表

        Returns:
            更新后的 StorageResult，或 None（表示未找到/无变化）
        """
        ...


class StorageError(Exception):
    """存储层通用异常基类"""


class StorageRateLimitError(StorageError):
    """速率限制，建议指数退避重试"""

    def __init__(self, message: str, retry_after: Optional[int] = None):
        super().__init__(message)
        self.retry_after = retry_after


class StorageAuthError(StorageError):
    """认证/授权错误，不建议重试"""


class StorageServerError(StorageError):
    """服务端/后端错误，建议重试"""

    def __init__(self, message: str, status_code: int = 500):
        super().__init__(message)
        self.status_code = status_code


StorageBackendFactory = Callable[..., StorageBackend]
_BACKEND_FACTORIES: Dict[str, StorageBackendFactory] = {}
_DEFAULT_BACKEND_PLUGIN_MODULES = ("integrations.backends",)


def register_storage_backend(name: str, factory: StorageBackendFactory) -> None:
    """Register a storage backend factory from adapter/plugin code."""
    normalized = str(name).strip().lower()
    if not normalized:
        raise ValueError("storage backend name cannot be empty")
    _BACKEND_FACTORIES[normalized] = factory


def clear_storage_backend_factories() -> None:
    """Clear registered factories; intended for tests and controlled reloads."""
    _BACKEND_FACTORIES.clear()


def _backend_plugin_modules() -> List[str]:
    raw = os.environ.get("MNEMOS_STORAGE_BACKEND_PLUGINS", "")
    if not raw.strip():
        return list(_DEFAULT_BACKEND_PLUGIN_MODULES)
    return [item.strip() for item in raw.split(",") if item.strip()]


def _load_storage_backend_plugins() -> None:
    """Load adapter-owned backend modules that register factories."""
    from core.import_guard import assert_allowed_module

    for module_name in _backend_plugin_modules():
        try:
            assert_allowed_module(module_name)
        except ValueError:
            logger.warning(
                "[storage_backend] 拒绝加载越界 StorageBackend 插件模块: %s", module_name
            )
            continue
        try:
            module = importlib.import_module(module_name)
            register = getattr(module, "register_storage_backends", None)
            if callable(register):
                register()
        except (OSError, ValueError, TypeError, KeyError, ImportError, AttributeError, RuntimeError):
            logger.debug("加载 StorageBackend 插件失败: %s", module_name, exc_info=True)


def create_storage_backend(backend_name: Optional[str] = None, **kwargs) -> StorageBackend:
    """
    根据配置统一创建 StorageBackend 实例。

    这是全库唯一的 StorageBackend 工厂入口。所有需要自动创建后端的
    模块都应调用本函数，而不是直接实例化 ObsidianBackend。

    Args:
        backend_name: 后端类型；None 时读取配置 ``storage.backend``，默认 obsidian。
        **kwargs: 透传给后端构造函数的额外参数（如 ``vault_path``）。

    Returns:
        StorageBackend 实例。

    Raises:
        ValueError: 配置的后端类型不受支持。
    """
    from core.config import get_config

    if backend_name is None:
        backend_name = getattr(get_config(), "storage_backend", "obsidian")
    backend_name = str(backend_name).strip().lower()
    if not backend_name:
        backend_name = "obsidian"

    if backend_name not in _BACKEND_FACTORIES:
        _load_storage_backend_plugins()

    factory = _BACKEND_FACTORIES.get(backend_name)
    if factory is not None:
        return factory(**kwargs)

    available = ", ".join(sorted(_BACKEND_FACTORIES)) or "none"
    raise ValueError(
        f"不支持的 storage backend: {backend_name!r}（已注册: {available}）"
    )
