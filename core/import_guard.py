# -*- coding: utf-8 -*-
"""动态导入白名单守卫。

防止用户可控的模块路径导致任意模块加载（S23）。
"""

from __future__ import annotations

_ALLOWED_MODULE_PREFIXES: tuple[str, ...] = ("core.", "integrations.")


def assert_allowed_module(module_path: str) -> None:
    """校验模块路径只允许 core.* 或 integrations.* 前缀。

    Args:
        module_path: 要导入的模块名。

    Raises:
        ValueError: 当模块路径不在允许白名单内时。
    """
    if not isinstance(module_path, str) or not module_path:
        raise ValueError("module_path must be a non-empty string")
    if not any(module_path.startswith(prefix) for prefix in _ALLOWED_MODULE_PREFIXES):
        raise ValueError(f"disallowed dynamic import target: {module_path}")


def is_allowed_module(module_path: str) -> bool:
    """返回模块路径是否在白名单内。"""
    try:
        assert_allowed_module(module_path)
        return True
    except ValueError:
        return False
