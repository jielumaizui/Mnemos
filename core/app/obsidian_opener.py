# -*- coding: utf-8 -*-
"""
ObsidianOpener — 跨平台 Obsidian 自动打开

支持两种打开方式：
1. 文件路径：open wiki/00-Dashboard.md（用 Obsidian 打开指定文件）
2. URI 协议：obsidian://open?vault=xxx&file=yyy（更精确的 Vault 定位）

跨平台：macOS (open)、Linux (xdg-open)、Windows (start)
"""

from __future__ import annotations

import logging
import os
import platform
import shutil
import subprocess
from pathlib import Path
from typing import Optional
from urllib.parse import quote

logger = logging.getLogger(__name__)


def open_obsidian(
    page_path: Optional[str] = None,
    vault_name: Optional[str] = None,
    uri: Optional[str] = None,
) -> bool:
    """
    打开 Obsidian 到指定页面。

    优先使用 obsidian:// URI 协议（精确 Vault 定位），
    回退到文件路径打开（系统默认应用）。

    Args:
        page_path: Wiki 页面相对路径，如 "00-Dashboard.md"
        vault_name: Obsidian Vault 名称（用于 URI 协议）
        uri: 完整 obsidian:// URI（最高优先级）

    Returns:
        是否成功触发打开
    """
    try:
        # 1. 优先使用传入的完整 URI
        if uri:
            return _open_uri(uri)

        # 2. 构建 obsidian:// URI
        if vault_name and page_path:
            obsidian_uri = _build_uri(vault_name, page_path)
            if obsidian_uri and _open_uri(obsidian_uri):
                return True
            # URI 失败，回退到文件路径

        # 3. 回退：用系统默认应用打开文件
        if page_path:
            return _open_file(page_path)

        # 4. 只打开 Obsidian 应用本身
        return _open_app()

    except (OSError, ValueError) as e:
        logger.error("打开 Obsidian 失败: %s", e, exc_info=True)
        return False


def _resolve_executable(name: str, fallback: Optional[str] = None) -> Optional[str]:
    """将命令名解析为绝对路径；未找到时返回存在的 fallback。"""
    resolved = shutil.which(name)
    if resolved:
        return resolved
    if fallback and Path(fallback).exists():
        return fallback
    return None


def _build_uri(vault_name: str, page_path: str) -> str:
    """构建 obsidian:// URI"""
    # 移除 .md 后缀（Obsidian URI 使用无后缀路径）
    file_name = page_path[:-3] if page_path.endswith(".md") else page_path
    encoded_vault = quote(vault_name)
    encoded_file = quote(file_name)
    return f"obsidian://open?vault={encoded_vault}&file={encoded_file}"


def _open_uri(uri: str) -> bool:
    """用系统命令打开 URI"""
    system = platform.system()
    try:
        if system == "Darwin":
            open_cmd = _resolve_executable("open", "/usr/bin/open")
            if not open_cmd:
                return False
            result = subprocess.run(
                [open_cmd, uri],
                capture_output=True,
                text=True,
                timeout=10,
            )
        elif system == "Windows":
            cmd_path = _resolve_executable("cmd.exe")
            if not cmd_path:
                system_root = os.environ.get("SystemRoot", r"C:\Windows")
                cmd_path = os.path.join(system_root, "System32", "cmd.exe")
            result = subprocess.run(
                [cmd_path, "/c", "start", uri],
                capture_output=True,
                text=True,
                timeout=10,
            )
        else:  # Linux
            xdg_cmd = _resolve_executable("xdg-open", "/usr/bin/xdg-open")
            if not xdg_cmd:
                return False
            result = subprocess.run(
                [xdg_cmd, uri],
                capture_output=True,
                text=True,
                timeout=10,
            )
        if result.returncode == 0:
            logger.info("已打开 Obsidian URI: %s", uri)
            return True
        else:
            logger.debug("URI 打开返回非零: %s %s", result.returncode, result.stderr)
            return False
    except FileNotFoundError:
        logger.debug("系统打开命令不可用", exc_info=True)
        return False
    except subprocess.TimeoutExpired:
        logger.debug("URI 打开超时", exc_info=True)
        return False


def _open_file(page_path: str) -> bool:
    """用系统默认应用打开 Wiki 文件"""
    from core.config import get_config

    config = get_config()

    full_path = config.wiki_dir / page_path
    if not full_path.exists():
        # 尝试加 .md 后缀
        full_path = config.wiki_dir / f"{page_path}.md"
    if not full_path.exists():
        logger.warning("页面不存在: %s", page_path)
        return False

    system = platform.system()
    try:
        if system == "Darwin":
            # macOS: open -a Obsidian 优先，回退到 open
            open_cmd = _resolve_executable("open", "/usr/bin/open")
            if not open_cmd:
                return False
            result = subprocess.run(
                [open_cmd, "-a", "Obsidian", str(full_path)],
                capture_output=True,
                text=True,
                timeout=10,
            )
            if result.returncode != 0:
                result = subprocess.run(
                    [open_cmd, str(full_path)],
                    capture_output=True,
                    text=True,
                    timeout=10,
                )
        elif system == "Windows":
            cmd_path = _resolve_executable("cmd.exe")
            if not cmd_path:
                system_root = os.environ.get("SystemRoot", r"C:\Windows")
                cmd_path = os.path.join(system_root, "System32", "cmd.exe")
            result = subprocess.run(
                [cmd_path, "/c", "start", str(full_path)],
                capture_output=True,
                text=True,
                timeout=10,
            )
        else:  # Linux
            xdg_cmd = _resolve_executable("xdg-open", "/usr/bin/xdg-open")
            if not xdg_cmd:
                return False
            result = subprocess.run(
                [xdg_cmd, str(full_path)],
                capture_output=True,
                text=True,
                timeout=10,
            )

        if result.returncode == 0:
            logger.info("已打开页面: %s", full_path)
            return True
        else:
            logger.warning("文件打开返回非零: %s", result.returncode)
            return False

    except FileNotFoundError:
        logger.warning("系统打开命令不可用", exc_info=True)
        return False
    except subprocess.TimeoutExpired:
        logger.warning("文件打开超时", exc_info=True)
        return False


def _open_app() -> bool:
    """只打开 Obsidian 应用（不指定页面）"""
    system = platform.system()
    try:
        if system == "Darwin":
            open_cmd = _resolve_executable("open", "/usr/bin/open")
            if not open_cmd:
                return False
            result = subprocess.run(
                [open_cmd, "-a", "Obsidian"],
                capture_output=True,
                text=True,
                timeout=10,
            )
        elif system == "Windows":
            # Windows: 尝试常见安装路径
            app_paths = [
                str(Path.home() / "AppData" / "Local" / "Obsidian" / "Obsidian.exe"),
            ]
            for app_path in app_paths:
                if Path(app_path).exists():
                    result = subprocess.run(
                        [app_path],
                        capture_output=True,
                        text=True,
                        timeout=10,
                    )
                    if result.returncode == 0:
                        return True
            # 回退
            cmd_path = _resolve_executable("cmd.exe")
            if not cmd_path:
                system_root = os.environ.get("SystemRoot", r"C:\Windows")
                cmd_path = os.path.join(system_root, "System32", "cmd.exe")
            result = subprocess.run(
                [cmd_path, "/c", "start", "obsidian://open"],
                capture_output=True,
                text=True,
                timeout=10,
            )
        else:  # Linux
            obsidian_cmd = _resolve_executable("obsidian")
            if not obsidian_cmd:
                return False
            result = subprocess.run(
                [obsidian_cmd],
                capture_output=True,
                text=True,
                timeout=10,
            )

        return result.returncode == 0
    except (OSError, ValueError):
        logging.getLogger(__name__).warning(
            "Caught unexpected error at obsidian_opener.py", exc_info=True
        )
        return False
