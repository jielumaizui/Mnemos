"""Obsidian vault registry helpers shared by core and backend adapters."""

from __future__ import annotations

import json
import logging
import os
import sys
import time
import uuid
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)


def obsidian_config_path() -> Optional[Path]:
    """Return Obsidian's vault registry path for the current platform."""
    override = os.environ.get("MNEMOS_OBSIDIAN_CONFIG_PATH")
    if override:
        return Path(override)
    if sys.platform == "darwin":
        return Path.home() / "Library/Application Support/Obsidian/obsidian.json"
    if sys.platform == "win32":
        return Path.home() / "AppData/Roaming/Obsidian/obsidian.json"
    return Path.home() / ".config/Obsidian/obsidian.json"


def ensure_vault_recognized(vault_path: Path) -> bool:
    """Ensure Obsidian can see ``vault_path`` without leaking adapter imports into core."""
    vault_path = Path(vault_path).expanduser()
    (vault_path / ".obsidian").mkdir(parents=True, exist_ok=True)

    config_path = obsidian_config_path()
    if not config_path or not config_path.exists():
        return False

    try:
        data = json.loads(config_path.read_text(encoding="utf-8"))
        vaults = data.get("vaults", {})
        vault_path_str = str(vault_path)

        for vault in vaults.values():
            if vault.get("path") == vault_path_str:
                return True

        new_id = str(uuid.uuid4()).replace("-", "")[:16]
        vaults[new_id] = {
            "path": vault_path_str,
            "ts": int(time.time() * 1000),
        }
        data["vaults"] = vaults
        config_path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        logger.info("Registered Obsidian vault: %s", vault_path_str)
        return True
    except (OSError, ValueError, TypeError, KeyError, ImportError, AttributeError, RuntimeError):
        logger.debug("Failed to register Obsidian vault", exc_info=True)
        return False


def is_vault_registered(vault_path: Path) -> bool:
    """Return whether ``vault_path`` is already present in Obsidian's registry."""
    vault_path = Path(vault_path).expanduser()
    config_path = obsidian_config_path()
    if not config_path or not config_path.exists():
        return False
    try:
        data = json.loads(config_path.read_text(encoding="utf-8"))
        vault_path_str = str(vault_path)
        for vault in data.get("vaults", {}).values():
            if vault.get("path") == vault_path_str:
                return True
    except (OSError, ValueError, TypeError, KeyError, ImportError, AttributeError, RuntimeError):
        logger.debug("Failed to read Obsidian vault registry", exc_info=True)
    return False
