# -*- coding: utf-8 -*-
"""
integrations/backends — 存储后端实现集合

提供：
- ObsidianBackend: 本地 Markdown 文件存储（无长度限制，frontmatter 驱动）
"""

from .obsidian_backend import ObsidianBackend


def _create_obsidian_backend(**kwargs):
    return ObsidianBackend(**kwargs)


def register_storage_backends() -> None:
    from core.sync_framework.storage_backend import register_storage_backend

    register_storage_backend("obsidian", _create_obsidian_backend)


register_storage_backends()

__all__ = ["ObsidianBackend", "register_storage_backends"]
