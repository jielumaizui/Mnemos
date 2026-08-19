"""Mnemos Vault 标准目录结构定义与初始化工具。

主认知 Vault（mnemos）与 Raw Vault（raw）的目录结构在此统一维护，
避免 init.py / auto_setup.py / mnemos_daemon.py 三处重复定义。
"""

from pathlib import Path
from typing import Iterable, Tuple

# 主认知 Vault 标准子目录
MNEMOS_VAULT_DIRS: Tuple[str, ...] = (
    # L2 Wiki
    "00-Inbox",
    "01-People",
    "02-Projects",
    "03-Tech",
    "04-Concepts",
    "05-MOCs",
    "06-Retrospectives",
    "07-Shadow",
    "99-Reports",
    # 认知层投影
    "L2.4-KG",
    "L2.4-KG/Entities",
    "L2.4-KG/Relations",
    "L2.4-KG/MOCs",
    "L3-Observations",
    "L4-Reflections",
    "L4-Reflections/Reflections",
    "L4-Reflections/Shifts",
    "L4-Reflections/Reports",
    "L5-Feedback",
)

INDEX_MD = """# Mnemos 知识库

自动生成的认知层入口。

- [[00-Inbox]]
- [[01-People]]
- [[02-Projects]]
- [[03-Tech]]
- [[04-Concepts]]
- [[05-MOCs]]
- [[06-Retrospectives]]
- [[L2.4-KG]]
- [[L3-Observations]]
- [[L4-Reflections]]
- [[L5-Feedback]]
"""


def init_mnemos_vault(mnemos_vault: Path) -> None:
    """创建主认知 Vault 的标准目录结构与入口索引。"""
    mnemos_vault.mkdir(parents=True, exist_ok=True)
    for d in MNEMOS_VAULT_DIRS:
        (mnemos_vault / d).mkdir(parents=True, exist_ok=True)

    index = mnemos_vault / "index.md"
    if not index.exists():
        index.write_text(INDEX_MD, encoding="utf-8")


def init_raw_vault(raw_vault: Path) -> None:
    """创建 Raw Vault 的 Obsidian 标记目录。"""
    raw_vault.mkdir(parents=True, exist_ok=True)
    (raw_vault / ".obsidian").mkdir(parents=True, exist_ok=True)


def init_vaults(mnemos_vault: Path, raw_vault: Path) -> None:
    """一次性初始化两个 Vault。"""
    init_mnemos_vault(mnemos_vault)
    init_raw_vault(raw_vault)


def default_mnemos_vault_path() -> Path:
    """返回首次设置时使用的主认知 Vault 默认路径。"""
    return Path.home() / "Documents" / "mnemos"


def default_raw_vault_path() -> Path:
    """返回首次设置时使用的 Raw Vault 默认路径。"""
    return Path.home() / "Documents" / "raw"


def list_mnemos_dirs() -> Iterable[str]:
    """返回主认知 Vault 的标准子目录列表（便于测试与校验）。"""
    return iter(MNEMOS_VAULT_DIRS)
