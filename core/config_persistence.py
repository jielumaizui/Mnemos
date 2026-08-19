"""Durable configuration-file paths and persistence boundaries.

This module intentionally knows nothing about the effective runtime Config
object.  It only owns the legacy source locations and the atomic write path so
configuration resolution stays separate from filesystem mutation.
"""

from __future__ import annotations

import json
import logging
import sys
from pathlib import Path
from typing import Any

from core.file_ops import atomic_write_text, secure_directory, secure_file
from core.runtime_environment import environment_get


def default_claude_settings_path() -> Path:
    """Return the platform-default Claude settings location without creating it."""
    candidates = [Path.home() / ".claude" / "settings.json"]
    if sys.platform == "win32":
        candidates.append(Path.home() / "AppData" / "Roaming" / "Claude" / "settings.json")
    elif sys.platform == "darwin":
        candidates.append(
            Path.home() / "Library" / "Application Support" / "Claude" / "settings.json"
        )
    else:
        candidates.append(Path.home() / ".config" / "claude" / "settings.json")
    return next((path for path in candidates if path.exists()), candidates[0])


def historical_config_paths(mnemos_dir: Path) -> list[Path]:
    """Return de-duplicated historical YAML locations in deterministic order."""
    paths = [mnemos_dir / "config.yaml"]
    if sys.platform == "win32":
        base = Path(environment_get("APPDATA", Path.home() / "AppData" / "Roaming"))
    elif sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support"
    else:
        base = Path.home() / ".config"
    paths.append(base / "mnemos" / "config.yaml")
    return list(dict.fromkeys(paths))


def load_historical_config(
    *,
    mnemos_dir: Path,
    config_path: Path,
    log: logging.Logger,
) -> dict[str, Any]:
    """Load one historical YAML source for migration, never for runtime authority."""
    try:
        import yaml
    except ImportError as exc:
        log.warning("旧配置读取失败: yaml 不可用: %s", exc)
        return {}

    for legacy_path in historical_config_paths(mnemos_dir):
        if not legacy_path.exists():
            continue
        try:
            with legacy_path.open("r", encoding="utf-8") as source:
                loaded = yaml.safe_load(source) or {}
        except (OSError, UnicodeError, yaml.YAMLError) as exc:
            log.warning("旧配置读取失败 %s: %s", legacy_path, exc)
            continue
        if isinstance(loaded, dict):
            log.info("检测到旧配置文件 %s，将迁移到 %s", legacy_path, config_path)
            return loaded
    return {}


def write_config_file(config_path: Path, data: dict[str, Any], provision: bool) -> None:
    """Atomically replace an explicitly authorized persisted JSON document."""
    if not provision:
        raise RuntimeError("non-provisioning Config cannot write persistent state")
    config_path.parent.mkdir(parents=True, exist_ok=True)
    secure_directory(config_path.parent)
    atomic_write_text(
        config_path,
        json.dumps(data, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    secure_file(config_path)
